# Komiic 漫画下载器

简体中文 | [English](README.en.md)

一个提供本地网页界面和命令行的 Komiic 漫画下载工具，支持单本与批量下载、断点续传，以及适用于 Panels 等阅读器的 CBZ 导出。章节和图片信息通过 Komiic 页面使用的 API 获取，无需滚动抓取页面。

## 功能

- **单本与批量下载**：选择章节、估算所需图片额度，批量队列支持运行中追加链接。
- **断点续传**：跳过已有图片，补齐未完成章节，下载后检查完整性并重试失败项。
- **账号与额度管理**：管理登录 Cookie、切换账号、查看剩余额度及历史变化。
- **漫画与任务记录**：查看本地漫画预览、关注漫画和下载任务状态。
- **CBZ 导出与复制**：完整章节自动打包，可手动复制或在下载完成后复制到指定目录。

支持 `komiic.com` 和 `komiic.cc`。请仅下载你有权访问的内容，并遵守站点的使用规则与访问限制。

## 快速开始

```bash
git clone https://github.com/lazymicezhu/komiic-download.git
cd komiic-download
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python gui_server.py
```

在浏览器打开 [http://127.0.0.1:8787](http://127.0.0.1:8787)。使用期间保持服务运行；下次启动时，在项目目录激活虚拟环境后运行 `python gui_server.py` 即可。

如需使用浏览器登录获取 Cookie，可安装 Playwright 浏览器：

```bash
python -m playwright install chromium
```

手动粘贴 Cookie 和命令行下载无需安装该浏览器。端口被占用时，可用 `PORT=8788 python gui_server.py` 指定其他端口。

## 网页界面

1. 在“账号池”中添加自己的登录 Cookie，或通过浏览器登录获取 Cookie。
2. 选择站点和输出目录，输入漫画链接并获取章节。
3. 勾选所需章节后开始下载，通过日志查看进度和错误。

批量下载时，每行输入一个漫画链接，可先估算额度，再开始下载；运行期间支持追加链接。程序按顺序处理漫画，跳过已完整章节，并在结束后检查缺失章节。

账号池在两个站点间共享。当前账号额度耗尽时，程序会尝试其他已启用账号。额度记录在服务运行期间定期更新。

“历史记录”可查看漫画预览、重新获取章节和标记关注；“下载任务”可查看任务状态。关注是便于筛选的标记，不会自动下载更新。历史预览使用默认下载目录，建议与实际输出目录保持一致。

## 下载目录与 CBZ

默认下载到项目的 `downloads` 目录，图片按漫画和章节整理：

```text
downloads/
└── 漫画名/
    ├── 漫画名_01_19p/
    │   ├── 001.jpg
    │   └── ...
    └── 漫画名_panels_cbz/
        └── 漫画名_01_19p.cbz
```

只有图片完整的章节才会生成 CBZ。重新运行同一任务时会跳过已有图片，因此续传时应保持输出目录不变。

如需设置默认下载位置或将 CBZ 复制到阅读器的导入目录，在项目根目录创建 `.komiic_local_paths.json`：

```json
{
  "downloadDir": "downloads",
  "cbzCopyTarget": "/path/to/CBZ-Inbox"
}
```

| 配置 | 说明 |
| --- | --- |
| `downloadDir` | GUI 默认下载目录；相对路径以项目根目录为基准，也可使用绝对路径。 |
| `cbzCopyTarget` | CBZ 复制目标；替换为自己的绝对路径，并提前创建目录。 |

修改后重启服务。可通过“复制 CBZ”按钮或下载完成后的复制选项，将新增 CBZ 复制到目标目录；源文件会保留。关闭复制不影响章节自动打包。

## 命令行

下载整部漫画：

```bash
python komiic_downloader.py "https://komiic.com/comic/1088" -o downloads
```

下载指定章节，章节 ID 可从章节链接的 `/chapter/{id}/` 获取：

```bash
python komiic_downloader.py "https://komiic.com/comic/1088" \
  -o downloads --chapter-ids 19836,19837,19838
```

下载单个章节，或将已有下载导出为 CBZ：

```bash
python komiic_downloader.py "章节链接" -o downloads --single-chapter
python komiic_downloader.py --export-existing-cbz downloads
```

登录凭证通过环境变量传入：

```bash
KOMIIC_COOKIE='这里粘贴 Cookie 请求头的值' \
python komiic_downloader.py "https://komiic.com/comic/1088" -o downloads
```

| 常用参数 | 说明 |
| --- | --- |
| `--base-url` | 选择 `https://komiic.com` 或 `https://komiic.cc`；使用 `.cc` 链接时也需指定对应站点。 |
| `--max-chapters` | 最多下载的章节数，默认 `0` 表示全部。 |
| `--chapter-delay` | 章节间隔秒数，默认 `5`。 |
| `--image-delay` | 图片间隔秒数，默认 `1`。 |
| `--image-retries` | 每张图片的立即尝试次数，默认 `2`。 |
| `--retry-delay` | 立即重试的基础等待秒数，默认 `2`。 |

运行 `python komiic_downloader.py --help` 查看全部参数。独立 CLI 不读取 GUI 的账号池或路径配置，请通过 `KOMIIC_COOKIE` 和 `-o` 指定。

## 常见问题

- **下载失败或连接被重置**：先查看日志。程序会重试失败图片；仍失败时，可稍后用相同目录重新运行以续传。
- **出现 HTTP 429 或额度不足**：等待限流冷却或额度重置后继续，已有文件会保留。
- **CBZ 没有生成**：检查对应章节是否仍有缺失图片。
- **CBZ 复制失败**：检查目标目录是否已配置、存在且可写；修改路径配置后需重启服务。

## 本地数据

账号 Cookie、额度与下载历史保存在 `.komiic_gui_config.json`，Cookie 为明文；浏览器登录资料保存在 `.komiic_playwright_profile/`。这些文件、路径配置和下载内容已被 Git 忽略，请保留在本机，不要上传或分享登录凭证。
