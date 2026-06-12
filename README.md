# Komiic 漫画下载器

一个用于学习测试的 Komiic 漫画下载工具，提供本地 GUI 页面和 CLI 两种方式。当前版本只使用 Komiic 页面实际调用的 API 获取章节、图片列表和图片 ticket，不再依赖浏览器页面渲染或滚动抓取。

请只在你已获得授权的前提下使用；不要绕过登录、付费、访问限制或反爬策略。默认配置会限速，并跳过已存在文件，方便断点续下。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 启动 GUI

```bash
python gui_server.py
```

然后打开：

```text
http://127.0.0.1:8787
```

GUI 支持：

- 输入漫画详情页链接或章节链接。
- 在 `https://komiic.cc/`（国内直连）和 `https://komiic.com/` 之间切换。
- 输入登录 Cookie，用授权账号请求 API；查询额度成功后会保存到本机项目配置。
- 查询当前账号剩余图片额度。
- 额度记录页面会每 1 分钟自动拉取并保存额度变化。
- 历史记录页面会显示已保存漫画的前三张本地预览图，可一键选择漫画并获取章节检查更新，也可以删除历史记录。
- 设置或选择输出目录。频繁更改输出目录会导致程序读不到旧目录，可能把已下载章节判断为未下载并重复下载。
- 获取章节列表，并勾选指定章节下载。
- 实时查看下载日志。
- 停止当前任务。

## CLI 下载整部漫画

```bash
python komiic_downloader.py "https://komiic.com/comic/1088" -o downloads
```

默认会下载全部已有章节。

如果要使用国内直连站点：

```bash
python komiic_downloader.py "https://komiic.cc/comic/1088" -o downloads --base-url https://komiic.cc
```

## CLI 只测试一个章节

```bash
python komiic_downloader.py "章节链接" -o downloads/test_chapter --single-chapter
```

## CLI 下载指定章节

先在 GUI 里点“获取章节”查看章节 ID，或从章节链接里取 `/chapter/{id}/` 的 ID。多个章节用英文逗号分隔：

```bash
python komiic_downloader.py "https://komiic.com/comic/1088" \
  -o downloads \
  --chapter-ids 19836,19837,19838
```

## 使用登录 Cookie

不要在聊天里发送账号密码或 Cookie。Cookie 等同于登录凭证，只在本机 GUI 或终端里使用。

GUI 会在“查询剩余额度”成功后，把当前站点的 Cookie 明文保存到项目本地 `.komiic_gui_config.json`。该文件已加入 `.gitignore`，但仍应只在你的本机保存。

在浏览器里复制 Cookie 的常用方式：

1. 登录 Komiic。
2. 打开开发者工具。
3. 进入 Network/网络面板。
4. 刷新页面，点开一个发往 `https://komiic.com/api/query` 的请求。
5. 在 Request Headers/请求标头里复制 `Cookie` 这一项的值。
6. 粘贴到 GUI 的“登录 Cookie”输入框。

CLI 也支持通过环境变量传入：

```bash
KOMIIC_COOKIE='这里粘贴 Cookie 请求头的值' \
python komiic_downloader.py "https://komiic.com/comic/1088" -o downloads/komiic_1088
```

## CLI 常用选项

```bash
python komiic_downloader.py "https://komiic.com/comic/1088" \
  --base-url https://komiic.com \
  --chapter-delay 5 \
  --image-delay 1 \
  --image-retries 2 \
  --retry-delay 2 \
  --max-chapters 3
```

- `--base-url`：选择 API 和 Referer 使用的站点，只支持 `https://komiic.com` 或 `https://komiic.cc`。
- `--single-chapter`：把传入链接当作章节页，只下载这一章。
- `--chapter-ids`：只下载指定章节 ID，多个 ID 用英文逗号分隔。
- `--max-chapters`：限制最多下载多少章；不传或传 `0` 表示下载全部已有章节。
- `--image-retries`：单张图片立即尝试次数，默认 `2`，也就是失败后立刻重试 1 次。
- `--retry-delay`：图片立即重试基础等待秒数，默认 `2`，每次重试会递增等待。章节末尾还会对失败图片整体补试 2 轮。

## 断点续传

下载目录会先按漫画名分组，再按稳定章节名保存：

```text
输出目录/漫画名/漫画名_章节名_几p
```

例如：

```text
downloads/在超市后门吸烟的二人/在超市后门吸烟的二人_01_19p/001.jpg
```

漫画名会优先使用 API 返回的标题，并通过 OpenCC 转为简体中文后用于目录名。

重新运行同一任务时，脚本会先检查本地已存在图片，只对缺失图片申请图片 ticket，避免因为已下载文件重复消耗图片额度。

旧版本生成的 `001_14 15p` 或 `14 15p_19849` 这类目录，如果能唯一匹配到当前章节，会自动迁移为新的稳定目录名。

## Panels / CBZ 导入

每个章节下载完成后，会自动在漫画目录下生成 Panels 可导入的 CBZ：

```text
downloads/漫画名/漫画名_panels_cbz/漫画名_01_19p.cbz
```

CBZ 内部只包含页码图片文件，例如 `001.jpg`、`002.jpg`，不包含外层目录或 `.DS_Store`。

只有章节图片完整时才会生成 CBZ；如果有图片最终下载失败，日志会列出失败页码并跳过该章节 CBZ。

如果要把已经下载好的章节批量生成 CBZ：

```bash
python komiic_downloader.py --export-existing-cbz downloads
```

## 运行注意

- 如果 GUI 页面显示任务失败，先看右侧日志。常见原因是网络不稳定、站点限流、Cookie 失效、图片 ticket 过期或图片额度不足。
- 如果日志里出现 `Connection reset by peer`，通常是图片 CDN 断开连接。当前版本会立即重试一次，并在章节末尾补试两轮。
- 如果日志里出现 `429 Too Many Requests`，表示图片 CDN 限速。当前版本会自动冷却等待后再试；继续频繁出现时，暂停一段时间后重新运行即可，已存在图片会跳过。
- 下载日志中的 `(53/66)` 表示当前章节第 53 张，共 66 张。
- 下载时会后台预取下一批图片 ticket，同时下载当前批次，减少“找到图片”后的等待。
