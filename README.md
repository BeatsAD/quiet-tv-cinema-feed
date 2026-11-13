# Quiet TV 每日电影

每天自动选择一部电影，补全简体中文资料，生成 1920×1080 静态 JPEG 和 V1 JSON，由 GitHub Pages 提供给 Quiet TV。电视无需 TMDB 密钥，启用后自动更新。

## 配置与运行

需要 Python 3.11 或更新版本。只在开发者侧配置自己的 TMDB API Read Access Token；不提交 `.env`、令牌文件或生成内容到源码分支。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python feed.py generate --token-file /绝对路径/私有令牌文件
.venv/bin/python feed.py verify
```

1. 把源码发布到独立仓库 `quiet-tv-cinema-feed` 的 `main`。
2. 在 Settings → Secrets and variables → Actions 配置 `TMDB_BEARER_TOKEN`。
3. Settings → Pages → Source 选择 GitHub Actions。允许工作流写入 `feed-data` 分支；`main` 仍通过 PR 和 Validate 管理。
4. 手动运行一次 Daily cinema feed，确认生成、部署和公网校验均成功。客户端地址为 `https://<用户名>.github.io/quiet-tv-cinema-feed/latest.json`。

每天北京时间 05:17 和 07:17 自动执行。同一天第二次运行复用完整快照，继续尝试部署；不会因为先前已经生成、但部署失败而跳过发布。GitHub 定时任务可能延迟或被取消，客户端前台每小时检查清单，休眠后恢复时补查，不依赖精确定时唤醒。

生成内容和去重历史保存在 `feed-data`，源码在 `main`。发布目录只包含 `latest.json`、`days/`、`images/`、说明页与 TMDB 标识，不暴露内部状态、Git 元数据或令牌。流水线使用官方 Pages 上传和部署动作，不依赖提交触发 Pages 构建。PR 检查不读取密钥。

## 内容与质量

- 已上映、非成人、TMDB 评分至少 7、至少 500 人评分；近期热门和经典高分片混合，90 天不重复。
- 优先 `zh-CN` 标题和简介；缺失时读取中文译本并用 OpenCC 转简体。仍缺少中文则换片，无需人工翻译。短介绍从完整句子提取，不生成虚构影评或台词。
- 只接受语言标记为空的背景图，原图至少 1920×1080，比例 1.70–1.90；解码后再次检查，不放大低清图。转换为 sRGB JPEG，轻微居中裁切至 16:9，不超过 2 MiB。
- 任何上游、资料、图像或容量错误使本次工作流失败，保留上次公开内容。生成总量达到 800 MiB 前停止新增并提示维护，不自动删除历史文件。

`--refresh` 可在同一天自动另选一部片，递增 revision；用于修复内容，不降低 revision。若要回用旧片，可在维护 PR 中按协议重新发布更高 revision，保持原图与历史文件不可变。

## 数据协议

`latest.json` 含 `schema_version: 1`、单调递增的 `revision`、北京时间 `content_date`、`timezone`、带时区的 `published_at`；`movie` 含 TMDB id、中文标题、年份、简介、短介绍、语言、评分及来源链接；`image` 含同站相对路径、宽高、文件字节数和 SHA-256。历史快照命名 `days/YYYY-MM-DD-r<revision>.json`，图像以 SHA 命名，不覆盖旧字节。

客户端只在图片完整下载、原字节校验成功且后台操作仍有效后提交图片和文字；离线保留上一张。平台兼容性由 Quiet TV 的既有 JPEG 导入与 TLS 实现承担。

## 验证与限制

本地测试放在 `tests/`，不随 Quiet TV 源码公开策略自动发布；运行 `.venv/bin/python -m unittest discover -s tests -v`。公开 Validate 检查生产脚本语法、依赖和命令入口，不调用 TMDB。真实选片需要有效令牌；部署后工作流验证公网 JSON 与图片 SHA-256。

中国大陆访问 GitHub Pages 的表现取决于网络，不能把单次电脑连通视为所有电视可用。首期保留旧背景和错误提示；需要更稳定的分发时可将完整站点同步到自有静态存储并在客户端构建时配置 HTTPS 地址。

本项目通过 API 使用 TMDB 数据，使用前核对 [TMDB 使用条款](https://www.themoviedb.org/terms-of-use) 与 [API FAQ](https://developer.themoviedb.org/docs/faq)。This product uses the TMDB API but is not endorsed or certified by TMDB. API 可用不代表取得海报或剧照的再分发权，图片版权属于各自权利人；公开图像托管须按实际用途核对授权。
