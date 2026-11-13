"""Automatic Chinese cinema wallpaper feed. No credentials are written to output."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import sys
from datetime import date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zoneinfo import ZoneInfo

from opencc import OpenCC
from PIL import Image, ImageCms, ImageOps

ZONE = ZoneInfo("Asia/Shanghai")
MAX_IMAGE = 2 * 1024 * 1024
MAX_SITE = 800 * 1024 * 1024
HAN = re.compile(r"[\u3400-\u9fff]")
SHA = re.compile(r"[a-f0-9]{64}")
SIMPLIFY = OpenCC("t2s")


class FeedError(Exception):
    pass


class SourceError(FeedError):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def fetch(url, limit, token=None):
    class SameHostRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            if urlparse(newurl).hostname != urlparse(url).hostname or urlparse(newurl).scheme != "https":
                raise SourceError("Unexpected upstream redirect")
            return super().redirect_request(request, fp, code, msg, headers, newurl)
    headers = {"User-Agent": "QuietTV-CinemaFeed/1", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with build_opener(SameHostRedirect()).open(Request(url, headers=headers), timeout=25) as response:
            # API credentials must never follow a redirect to an unrelated host.
            if urlparse(response.url).hostname != urlparse(url).hostname:
                raise SourceError("Unexpected upstream redirect")
            body = response.read(limit + 1)
            if not body or len(body) > limit:
                raise SourceError("Upstream response has invalid size")
            return body
    except HTTPError as error:
        # Do not include request headers, token, response body or signed URLs.
        raise SourceError("Upstream HTTP " + str(error.code)) from None
    except (URLError, TimeoutError, OSError):
        raise SourceError("Upstream network unavailable") from None


class Tmdb:
    def __init__(self, token):
        if not token:
            raise FeedError("Set TMDB_BEARER_TOKEN or --token-file (never pass tokens in arguments)")
        self.token = token

    def get(self, path, **params):
        url = "https://api.themoviedb.org/3/" + path + "?" + urlencode(params)
        try:
            return json.loads(fetch(url, 2 * 1024 * 1024, self.token))
        except (ValueError, TypeError):
            raise SourceError("Invalid TMDB JSON") from None

    def candidates(self, day):
        items = {}
        for sort in ("popularity.desc", "vote_average.desc"):
            # Rotate discovery pages as well as ordering, so 90-day exclusions do not exhaust a fixed short list.
            pages = [1] + random.Random(day.isoformat() + sort).sample(range(2, 26), 4)
            for page in pages:
                result = self.get("discover/movie", language="zh-CN", include_adult="false",
                                  include_video="false", sort_by=sort, page=page,
                                  **{"vote_average.gte": 7, "vote_count.gte": 500,
                                     "primary_release_date.lte": day.isoformat()})
                for item in result.get("results", []):
                    if eligible(item, day):
                        items[item["id"]] = item
        result = list(items.values())
        random.Random(day.isoformat()).shuffle(result)
        return result

    def detail(self, movie_id):
        return self.get(f"movie/{movie_id}", language="zh-CN")

    def translations(self, movie_id):
        return self.get(f"movie/{movie_id}/translations").get("translations", [])

    def images(self, movie_id):
        return self.get(f"movie/{movie_id}/images", include_image_language="null").get("backdrops", [])

    def image(self, path):
        if not re.fullmatch(r"/[A-Za-z0-9_-]+\.(jpg|png)", path):
            raise FeedError("Invalid TMDB image path")
        return fetch("https://image.tmdb.org/t/p/original" + path, 24 * 1024 * 1024)


def eligible(movie, day):
    try:
        released = date.fromisoformat(movie["release_date"])
        return (type(movie["id"]) is int and movie["id"] > 0 and movie.get("adult") is False
                and movie["vote_average"] >= 7 and movie["vote_count"] >= 500
                and released <= day)
    except (KeyError, ValueError, TypeError):
        return False


def clean(value):
    return SIMPLIFY.convert(re.sub(r"\s+", " ", str(value or "")).strip())


def summary(overview):
    sentences = re.findall(r"[^。！？!?]+[。！？!?]", overview)
    result = ""
    for sentence in sentences:
        if len(result + sentence) > 120:
            break
        result += sentence
        if len(result) >= 45:
            break
    return result.strip()


def chinese(detail, translations=()):
    title, overview = clean(detail.get("title")), clean(detail.get("overview"))
    order = {"CN": 0, "SG": 1, "TW": 2, "HK": 3}
    variants = sorted((item for item in translations if item.get("iso_639_1") == "zh"),
                      key=lambda item: order.get(item.get("iso_3166_1"), 4))
    for item in variants:
        data = item.get("data", {})
        if not HAN.search(title):
            title = clean(data.get("title"))
        if not HAN.search(overview):
            overview = clean(data.get("overview"))
    short = summary(overview)
    if not HAN.search(title) or not HAN.search(overview) or not short or len(title) > 100 or len(overview) > 4000:
        raise FeedError("Complete Chinese metadata unavailable")
    return title, overview, short


def ranked_images(images):
    good = []
    for image in images:
        try:
            width, height = image["width"], image["height"]
            if (image.get("iso_639_1") is None and 1920 <= width <= 9000
                    and 1080 <= height <= 9000 and 1.70 <= width / height <= 1.90):
                good.append(image)
        except (KeyError, TypeError, ZeroDivisionError):
            continue
    return sorted(good, key=lambda image: (image.get("vote_count", 0) > 0,
                  image.get("vote_average", 0), image.get("vote_count", 0), image["width"]), reverse=True)


def jpeg(raw):
    try:
        with Image.open(io.BytesIO(raw)) as original:
            width, height = original.size
            if width > 9000 or height > 9000 or width < 1920 or height < 1080 or not 1.70 <= width / height <= 1.90:
                raise FeedError("Image is not a full-HD landscape")
            original.load()
            image = ImageOps.exif_transpose(original)
            width, height = image.size
            if width < 1920 or height < 1080 or not 1.70 <= width / height <= 1.90:
                raise FeedError("Image is not a full-HD landscape")
            profile = image.info.get("icc_profile")
            if profile:
                image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                                                  ImageCms.createProfile("sRGB"), outputMode="RGB")
            else:
                image = image.convert("RGB")
            image = ImageOps.fit(image, (1920, 1080), Image.Resampling.LANCZOS)
            for quality in (90, 85, 80, 75):
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=quality, optimize=True,
                           icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                result = output.getvalue()
                if len(result) <= MAX_IMAGE:
                    return result
    except (OSError, ValueError, Image.DecompressionBombError, ImageCms.PyCMSError):
        raise FeedError("Image cannot be decoded or converted") from None
    raise FeedError("JPEG exceeds 2 MiB")


def validate(feed):
    try:
        if type(feed["schema_version"]) is not int or feed["schema_version"] != 1:
            raise ValueError()
        if type(feed["revision"]) is not int or feed["revision"] < 1 or feed["revision"] > 9007199254740991:
            raise ValueError()
        date.fromisoformat(feed["content_date"])
        if feed["timezone"] != "Asia/Shanghai":
            raise ValueError()
        if datetime.fromisoformat(feed["published_at"]).utcoffset() is None:
            raise ValueError()
        movie, image = feed["movie"], feed["image"]
        if movie["provider"] != "tmdb" or type(movie["id"]) is not int or movie["id"] < 1:
            raise ValueError()
        if movie["text_locale"] != "zh-CN":
            raise ValueError()
        for key, limit in (("title", 100), ("overview", 4000), ("summary", 120)):
            if not isinstance(movie[key], str) or not HAN.search(movie[key]) or not 0 < len(movie[key]) <= limit:
                raise ValueError()
        if not 1800 <= movie["year"] <= 2200 or not 0 <= movie["rating"]["value"] <= 10:
            raise ValueError()
        if movie["rating"]["provider"] != "TMDB" or movie["rating"]["vote_count"] < 500:
            raise ValueError()
        if movie["source_url"] != "https://www.themoviedb.org/movie/" + str(movie["id"]):
            raise ValueError()
        if not SHA.fullmatch(image["sha256"]) or image["path"] != f'images/{movie["id"]}/{image["sha256"]}.jpg':
            raise ValueError()
        if image["width"] != 1920 or image["height"] != 1080 or not 0 < image["bytes"] <= MAX_IMAGE:
            raise ValueError()
        return feed
    except (KeyError, ValueError, TypeError, OverflowError):
        raise FeedError("Invalid feed V1") from None


def verify_local(root, feed):
    validate(feed)
    raw = (root / feed["image"]["path"]).read_bytes()
    if len(raw) != feed["image"]["bytes"] or hashlib.sha256(raw).hexdigest() != feed["image"]["sha256"]:
        raise FeedError("Published image checksum mismatch")
    with Image.open(io.BytesIO(raw)) as image:
        image.verify()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    with temporary.open("wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def generate(root, api, now, refresh=False):
    root = Path(root)
    day = now.astimezone(ZONE).date()
    state_path = root / "state.json"
    state = read_json(state_path) if state_path.exists() else {"revision": 0, "history": []}
    latest_path = root / "latest.json"
    if latest_path.exists():
        latest = read_json(latest_path)
        verify_local(root, latest)
        if latest["revision"] != state["revision"]:
            raise FeedError("State and latest revision differ; repair before publishing")
        if date.fromisoformat(latest["content_date"]) > day:
            raise FeedError("Refusing to move content date backwards")
        if latest["content_date"] == day.isoformat() and not refresh:
            return latest
    elif state["revision"]:
        raise FeedError("State exists without latest.json")
    recent = {item["id"] for item in state["history"]
              if date.fromisoformat(item["date"]) >= day - timedelta(days=90)}
    for candidate in api.candidates(day)[:100]:
        if candidate["id"] in recent:
            continue
        try:
            detail = api.detail(candidate["id"])
            if not eligible(detail, day):
                continue
            try:
                title, overview, short = chinese(detail)
            except FeedError:
                title, overview, short = chinese(detail, api.translations(detail["id"]))
            for image in ranked_images(api.images(detail["id"]))[:5]:
                try:
                    raw = jpeg(api.image(image["file_path"]))
                    break
                except FeedError:
                    raw = None
            else:
                raw = None
            if raw is None:
                continue
        except SourceError:
            raise  # Network/authentication/rate-limit errors do not churn through 100 films.
        except FeedError:
            continue
        digest = hashlib.sha256(raw).hexdigest()
        feed = validate({"schema_version": 1, "revision": state["revision"] + 1,
            "content_date": day.isoformat(), "timezone": "Asia/Shanghai", "published_at": now.astimezone(ZONE).isoformat(),
            "movie": {"provider": "tmdb", "id": detail["id"], "title": title,
                      "year": int(detail["release_date"][:4]), "overview": overview, "summary": short,
                      "text_locale": "zh-CN", "rating": {"provider": "TMDB", "value": round(detail["vote_average"], 1),
                      "vote_count": detail["vote_count"]}, "source_url": f'https://www.themoviedb.org/movie/{detail["id"]}'},
            "image": {"path": f"images/{detail['id']}/{digest}.jpg", "width": 1920, "height": 1080,
                      "bytes": len(raw), "sha256": digest}})
        used = sum(path.stat().st_size for folder in (root / "images", root / "days")
                   for path in folder.rglob("*") if path.is_file())
        used += sum(path.stat().st_size for path in (latest_path, state_path) if path.is_file())
        if used + len(raw) + len(encoded(feed)) * 2 + len(encoded(state)) > MAX_SITE:
            raise FeedError("Feed storage reaches 800 MiB; previous publication retained")
        image_path = root / feed["image"]["path"]
        if image_path.exists():
            if image_path.read_bytes() != raw:
                raise FeedError("Immutable image file differs from its checksum")
        else:
            atomic(image_path, raw)
        atomic(root / "days" / f"{day.isoformat()}-r{feed['revision']}.json", encoded(feed))
        # Only a successful full run is committed/pushed by the workflow.
        state = {"revision": feed["revision"], "history": state["history"] +
                 [{"date": day.isoformat(), "id": detail["id"], "revision": feed["revision"]}]}
        atomic(latest_path, encoded(feed))
        atomic(state_path, encoded(state))
        return feed
    raise FeedError("No eligible movie with Chinese text and full-HD textless backdrop; previous feed retained")


def verify_remote(base, expected):
    base = base.rstrip("/") + "/"
    if urlparse(base).scheme != "https":
        raise FeedError("Public URL requires HTTPS")
    published = validate(json.loads(fetch(urljoin(base, "latest.json") + "?revision=" + str(expected["revision"]), 256 * 1024)))
    if published != expected:
        raise FeedError("Public feed differs from generated snapshot")
    raw = fetch(urljoin(base, published["image"]["path"]), MAX_IMAGE)
    if len(raw) != published["image"]["bytes"] or hashlib.sha256(raw).hexdigest() != published["image"]["sha256"]:
        raise FeedError("Public image checksum mismatch")


def audit(api, now, sample=30):
    """Inspect real candidates without writing or changing a published snapshot."""
    if not 1 <= sample <= 100:
        raise FeedError("Audit sample must be between 1 and 100")
    day = now.astimezone(ZONE).date()
    candidates = api.candidates(day)
    if len(candidates) < sample:
        raise FeedError("Not enough eligible candidates for the requested audit")
    items = []
    for candidate in candidates[:sample]:
        detail = api.detail(candidate["id"])
        item = {"id": candidate["id"], "eligible": eligible(detail, day),
                "chinese": False, "hd_backdrop": False, "jpeg_ready": False}
        try:
            try:
                title, _, _ = chinese(detail)
            except FeedError:
                title, _, _ = chinese(detail, api.translations(candidate["id"]))
            item.update(title=title, chinese=True)
        except SourceError:
            raise
        except FeedError:
            pass
        images = ranked_images(api.images(candidate["id"]))
        item["hd_backdrop"] = bool(images)
        for image in images[:5]:
            try:
                raw = jpeg(api.image(image["file_path"]))
                item.update(jpeg_ready=True, original_width=image["width"],
                            original_height=image["height"], jpeg_bytes=len(raw))
                break
            except FeedError:
                continue
        item["usable"] = all(item[key] for key in ("eligible", "chinese", "jpeg_ready"))
        items.append(item)
    return {"content_date": day.isoformat(), "candidate_pool": len(candidates), "sample": len(items),
            **{key: sum(item[key] for item in items) for key in
               ("eligible", "chinese", "hd_backdrop", "jpeg_ready", "usable")}, "items": items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "verify", "verify-remote", "audit"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--refresh", action="store_true", help="Publish a new higher revision, even on the same day")
    parser.add_argument("--url")
    parser.add_argument("--sample", type=int, default=30, help="Number of candidates for the read-only quality audit")
    args = parser.parse_args()
    try:
        if args.command == "audit":
            token = args.token_file.read_text().strip() if args.token_file else os.environ.get("TMDB_BEARER_TOKEN", "").strip()
            print(json.dumps(audit(Tmdb(token), datetime.now(ZONE), args.sample), ensure_ascii=False, indent=2))
            return 0
        if args.command == "generate":
            # Same-day recovery/deploy works without a fresh upstream request.
            class LazyTmdb:
                def __getattr__(self, name):
                    token = args.token_file.read_text().strip() if args.token_file else os.environ.get("TMDB_BEARER_TOKEN", "").strip()
                    return getattr(Tmdb(token), name)
            feed = generate(args.data, LazyTmdb(), datetime.now(ZONE), args.refresh)
        else:
            feed = read_json(args.data / "latest.json")
            verify_local(args.data, feed)
            if args.command == "verify-remote":
                if not args.url:
                    raise FeedError("--url is required")
                verify_remote(args.url, feed)
        print(f"Verified revision {feed['revision']} for {feed['content_date']} (TMDB {feed['movie']['id']})")
    except (FeedError, OSError, ValueError, KeyError) as error:
        print("Feed failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
