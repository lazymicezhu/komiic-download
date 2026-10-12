from __future__ import annotations

import argparse
import json
import os
import re
import time
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    from opencc import OpenCC
except ImportError:  # pragma: no cover - optional runtime dependency
    OpenCC = None


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif")
CBZ_DIR_SUFFIX = "panels_cbz"
ALLOWED_BASE_URLS = ("https://komiic.com", "https://komiic.cc")
DEFAULT_BASE_URL = "https://komiic.com"
QUOTA_EXCEEDED_EXIT_CODE = 75
_OPENCC = None


@dataclass(frozen=True)
class Chapter:
    title: str
    url: str
    chapter_id: str
    comic_title: str = "unknown"
    serial: str = ""
    size: int = 0


@dataclass(frozen=True)
class ImageResource:
    url: str
    ticket: str
    kid: str


@dataclass(frozen=True)
class ImageMeta:
    index: int
    kid: str


class QuotaExceeded(RuntimeError):
    pass


class RateLimited(RuntimeError):
    pass


CHAPTERS_QUERY = """
query chapterByComicId($comicId: ID!) {
  chaptersByComicId(comicId: $comicId) {
    id
    serial
    type
    dateCreated
    dateUpdated
    size
    __typename
  }
}
"""

COMIC_QUERY = """
query comicById($comicId: ID!) {
  comicById(comicId: $comicId) {
    id
    title
    __typename
  }
}
"""

IMAGES_QUERY = """
query imagesByChapterId($chapterId: ID!) {
  imagesByChapterId(chapterId: $chapterId) {
    id
    kid
    height
    width
    __typename
  }
}
"""

TICKETS_QUERY = """
query getImageTickets($kids: [String!]!) {
  getImageTickets(kids: $kids) {
    ...ImageTicketFields
    __typename
  }
}

fragment ImageTicketFields on ImageTicket {
  url
  ticket
  kid
  width
  height
  expiresAt
  __typename
}
"""

IMAGE_LIMIT_QUERY = """
query getImageLimit {
  getImageLimit {
    limit
    usage
    resetInSeconds
    __typename
  }
}
"""


def safe_name(value: str, fallback: str = "untitled") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:100] or fallback).strip()


def parse_comic_id(url: str) -> str:
    match = re.search(r"/comic/(\d+)", url)
    return match.group(1) if match else ""


def parse_chapter_id(url: str) -> str:
    match = re.search(r"/chapter/(\d+)", url)
    return match.group(1) if match else ""


def normalize_base_url(value: str = "") -> str:
    base_url = (value or DEFAULT_BASE_URL).strip().rstrip("/")
    if base_url not in ALLOWED_BASE_URLS:
        allowed = ", ".join(ALLOWED_BASE_URLS)
        raise ValueError(f"不支持的站点地址: {base_url}，只能使用 {allowed}")
    return base_url


def api_url(base_url: str = DEFAULT_BASE_URL) -> str:
    return f"{normalize_base_url(base_url)}/api/query"


def site_referer(base_url: str = DEFAULT_BASE_URL) -> str:
    return f"{normalize_base_url(base_url)}/"


def image_url_for_base(url: str, base_url: str = DEFAULT_BASE_URL) -> str:
    base_url = normalize_base_url(base_url)
    if base_url == "https://komiic.cc":
        return url.replace("https://img.komiic.com/", "https://img.komiic.cc/")
    return url


def simplify_title(value: str) -> str:
    global _OPENCC
    if OpenCC is None:
        return value
    if _OPENCC is None:
        _OPENCC = OpenCC("t2s")
    return _OPENCC.convert(value)


def api_query(
    session: requests.Session,
    operation_name: str,
    variables: dict,
    query: str,
    base_url: str = DEFAULT_BASE_URL,
    retries: int = 5,
    retry_delay: float = 2.0,
) -> dict:
    attempts = max(int(retries), 1)
    response = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.post(
                api_url(base_url),
                data=json.dumps(
                    {
                        "operationName": operation_name,
                        "variables": variables,
                        "query": query,
                    }
                ),
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Connection": "close",
                    "Content-Type": "application/json",
                    "Origin": normalize_base_url(base_url),
                    "Referer": site_referer(base_url),
                },
                timeout=60,
            )
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            if attempt >= attempts:
                raise
            wait = retry_delay * attempt
            print(f"API 请求失败，{wait:.1f} 秒后重试 {attempt}/{attempts}: {operation_name}", flush=True)
            print(f"原因: {exc}", flush=True)
            time.sleep(wait)

    if response is None:
        raise RuntimeError("API 请求没有返回响应")

    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"API 返回错误: {payload['errors']}")
    return payload.get("data") or {}


def get_image_limit(session: requests.Session, base_url: str = DEFAULT_BASE_URL) -> dict:
    data = api_query(session, "getImageLimit", {}, IMAGE_LIMIT_QUERY, base_url=base_url)
    return data.get("getImageLimit") or {}


def get_comic_title(session: requests.Session, comic_id: str, base_url: str = DEFAULT_BASE_URL) -> str:
    data = api_query(session, "comicById", {"comicId": comic_id}, COMIC_QUERY, base_url=base_url)
    comic = data.get("comicById") or {}
    raw_title = str(comic.get("title") or f"comic_{comic_id}")
    return safe_name(simplify_title(raw_title))


def format_reset_time(seconds: object) -> str:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return "未知"

    hours, remainder = divmod(value, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def get_chapters(session: requests.Session, comic_url: str, base_url: str = DEFAULT_BASE_URL) -> list[Chapter]:
    base_url = normalize_base_url(base_url)
    comic_id = parse_comic_id(comic_url)
    if not comic_id:
        raise ValueError("漫画链接里没有找到 comic id")

    comic_title = get_comic_title(session, comic_id, base_url=base_url)
    data = api_query(session, "chapterByComicId", {"comicId": comic_id}, CHAPTERS_QUERY, base_url=base_url)
    chapters = []
    for item in data.get("chaptersByComicId") or []:
        if item.get("type") != "chapter":
            continue
        chapter_id = str(item["id"])
        serial = safe_name(str(item.get("serial") or chapter_id))
        size = int(item.get("size") or 0)
        title = safe_name(f"{serial} {size}p")
        url = f"{base_url}/comic/{comic_id}/chapter/{chapter_id}/images/all"
        chapters.append(
            Chapter(
                title=title,
                url=url,
                chapter_id=chapter_id,
                comic_title=comic_title,
                serial=serial,
                size=size,
            )
        )

    if not chapters:
        raise RuntimeError("API 没有返回可下载章节")
    return chapters


def get_chapter_image_metas(
    session: requests.Session,
    chapter_id: str,
    base_url: str = DEFAULT_BASE_URL,
) -> list[ImageMeta]:
    data = api_query(session, "imagesByChapterId", {"chapterId": chapter_id}, IMAGES_QUERY, base_url=base_url)
    return [
        ImageMeta(index=index, kid=item["kid"])
        for index, item in enumerate(data.get("imagesByChapterId") or [], start=1)
        if item.get("kid")
    ]


def get_image_tickets(
    session: requests.Session,
    metas: list[ImageMeta],
    base_url: str = DEFAULT_BASE_URL,
) -> dict[int, ImageResource]:
    images: list[ImageResource] = []
    cursor = 0
    while cursor < len(metas):
        limit_info = get_image_limit(session, base_url=base_url)
        limit = int(limit_info.get("limit") or 0)
        usage = int(limit_info.get("usage") or 0)
        remaining = max(limit - usage, 0)

        if remaining <= 0:
            reset = format_reset_time(limit_info.get("resetInSeconds"))
            raise QuotaExceeded(f"每日图片配额已用完: {usage}/{limit}，预计 {reset} 后重置")

        chunk_size = min(5, remaining, len(metas) - cursor)
        chunk = metas[cursor : cursor + chunk_size]
        ticket_data = api_query(
            session,
            "getImageTickets",
            {"kids": [meta.kid for meta in chunk]},
            TICKETS_QUERY,
            base_url=base_url,
        )
        for item in ticket_data.get("getImageTickets") or []:
            if item.get("url") and item.get("ticket"):
                images.append(
                    ImageResource(
                        url=image_url_for_base(str(item["url"]), base_url),
                        ticket=item["ticket"],
                        kid=item.get("kid", ""),
                    )
                )
        cursor += chunk_size
    images_by_kid = {image.kid: image for image in images}
    return {
        meta.index: images_by_kid[meta.kid]
        for meta in metas
        if meta.kid in images_by_kid
    }


def clone_session(session: requests.Session) -> requests.Session:
    cloned = requests.Session()
    cloned.headers.update(session.headers)
    return cloned


def guess_extension(url: str, content_type: str = "") -> str:
    path = urlparse(url).path.lower()
    for extension in IMAGE_EXTENSIONS:
        if extension in path:
            return extension

    content_type = content_type.lower()
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "avif" in content_type:
        return ".avif"
    return ".jpg"


def download_image(
    session: requests.Session,
    image: ImageResource,
    save_path: Path,
    base_url: str = DEFAULT_BASE_URL,
    retries: int = 5,
    retry_delay: float = 2.0,
    progress: str = "",
) -> bool:
    progress_text = f" {progress}" if progress else ""
    guessed_path = save_path.with_suffix(guess_extension(image.url))
    if guessed_path.exists() and guessed_path.stat().st_size > 0:
        print(f"  已存在，跳过{progress_text}: {guessed_path}", flush=True)
        return True

    headers = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
        "Referer": site_referer(base_url),
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
        "x-image-ticket": image.ticket,
    }
    attempts = max(int(retries), 1)
    response = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(image.url, headers=headers, timeout=60)
            response.raise_for_status()
            break
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if status_code == 429:
                raise RateLimited(f"图片 CDN 限速 429，已跳过并留到章节末尾重试: {image.url}") from exc
            if attempt >= attempts:
                raise
            wait = retry_delay * attempt
            print(f"  下载失败，{wait:.1f} 秒后重试 {attempt}/{attempts}: {image.url}", flush=True)
            print(f"  原因: {exc}", flush=True)
            time.sleep(wait)
        except requests.RequestException as exc:
            if attempt >= attempts:
                raise
            wait = retry_delay * attempt
            print(f"  下载失败，{wait:.1f} 秒后重试 {attempt}/{attempts}: {image.url}", flush=True)
            print(f"  原因: {exc}", flush=True)
            time.sleep(wait)

    if response is None:
        raise RuntimeError("图片请求没有返回响应")

    content_type = response.headers.get("content-type", "")
    final_path = save_path.with_suffix(guess_extension(image.url, content_type))
    if final_path.exists() and final_path.stat().st_size > 0:
        print(f"  已存在，跳过{progress_text}: {final_path}", flush=True)
        return True

    final_path.write_bytes(response.content)
    print(f"  下载成功{progress_text}: {final_path}", flush=True)
    return True


def existing_image_path(chapter_dir: Path, index: int) -> Path | None:
    base = chapter_dir / f"{index:03d}.jpg"
    for extension in IMAGE_EXTENSIONS:
        candidate = base.with_suffix(extension)
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def chapter_image_files(chapter_dir: Path) -> list[Path]:
    if not chapter_dir.exists() or not chapter_dir.is_dir():
        return []
    return sorted(
        [
            path
            for path in chapter_dir.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS and path.stat().st_size > 0
        ],
        key=lambda path: path.name,
    )


def cbz_dir_for_comic(comic_dir: Path) -> Path:
    cbz_dir = comic_dir / safe_name(f"{comic_dir.name}_{CBZ_DIR_SUFFIX}")
    legacy_dir = comic_dir / CBZ_DIR_SUFFIX
    if legacy_dir.exists() and legacy_dir.is_dir() and not cbz_dir.exists():
        legacy_dir.rename(cbz_dir)
    return cbz_dir


def cbz_path_for_chapter(chapter_dir: Path) -> Path:
    cbz_dir = cbz_dir_for_comic(chapter_dir.parent)
    return cbz_dir / f"{chapter_dir.name}.cbz"


def expected_images_from_dir_name(chapter_dir: Path) -> int:
    match = re.search(r"_(\d+)p$", chapter_dir.name)
    if not match:
        return 0
    return int(match.group(1))


def is_chapter_complete(chapter_dir: Path, total_images: int) -> bool:
    if total_images <= 0:
        return False
    return all(existing_image_path(chapter_dir, index) is not None for index in range(1, total_images + 1))


def export_chapter_cbz(chapter_dir: Path, expected_images: int = 0) -> Path | None:
    if expected_images > 0 and not is_chapter_complete(chapter_dir, expected_images):
        print(f"  图片不完整，跳过生成 CBZ: {chapter_dir}", flush=True)
        return None

    images = chapter_image_files(chapter_dir)
    if not images:
        return None

    cbz_path = cbz_path_for_chapter(chapter_dir)
    cbz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cbz_path.with_suffix(".cbz.tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for image in images:
            archive.write(image, arcname=image.name)

    tmp_path.replace(cbz_path)
    return cbz_path


def export_existing_cbz(output_dir: Path) -> int:
    if not output_dir.exists() or not output_dir.is_dir():
        raise ValueError(f"目录不存在: {output_dir}")

    exported = 0
    for comic_dir in sorted(path for path in output_dir.iterdir() if path.is_dir()):
        cbz_dir = cbz_dir_for_comic(comic_dir)
        for chapter_dir in sorted(path for path in comic_dir.iterdir() if path.is_dir()):
            if chapter_dir == cbz_dir or chapter_dir.name == CBZ_DIR_SUFFIX or chapter_dir.name.endswith(f"_{CBZ_DIR_SUFFIX}"):
                continue
            cbz_path = export_chapter_cbz(chapter_dir, expected_images=expected_images_from_dir_name(chapter_dir))
            if cbz_path:
                exported += 1
                print(f"已生成 CBZ: {cbz_path}", flush=True)
    return exported


def stable_chapter_dir(output_dir: Path, chapter: Chapter) -> Path:
    comic_dir = output_dir / safe_name(chapter.comic_title)
    stable = comic_dir / safe_name(f"{chapter.comic_title}_{chapter.serial}_{chapter.size}p")
    if stable.exists():
        return stable

    legacy_candidates = []
    for root in [output_dir, comic_dir]:
        legacy_candidates.extend(
            [
                root / safe_name(f"{chapter.title}_{chapter.chapter_id}"),
                root / safe_name(f"{chapter.comic_title}_{chapter.title}"),
            ]
        )
        legacy_candidates.extend(
            path
            for path in root.glob(f"???_{chapter.title}")
            if path.is_dir() and path.name[:3].isdigit()
        )

    unique_matches = list(dict.fromkeys(path for path in legacy_candidates if path.is_dir()))
    if len(unique_matches) == 1:
        comic_dir.mkdir(parents=True, exist_ok=True)
        unique_matches[0].rename(stable)
    return stable


def download_chapter(
    session: requests.Session,
    chapter: Chapter,
    chapter_dir: Path,
    image_delay: float,
    base_url: str = DEFAULT_BASE_URL,
    image_retries: int = 2,
    retry_delay: float = 2.0,
) -> None:
    chapter_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n开始处理章节: {chapter.title}", flush=True)
    print(chapter.url, flush=True)

    image_metas = get_chapter_image_metas(session, chapter.chapter_id, base_url=base_url)
    if not image_metas:
        print("  这一章没有找到图片", flush=True)
        return

    missing_metas = []
    for meta in image_metas:
        existing = existing_image_path(chapter_dir, meta.index)
        if existing:
            print(f"  已存在，跳过: {existing}", flush=True)
        else:
            missing_metas.append(meta)

    if not missing_metas:
        print(f"  找到 {len(image_metas)} 张图片，已全部下载，跳过申请 ticket", flush=True)
        cbz_path = export_chapter_cbz(chapter_dir, expected_images=len(image_metas))
        if cbz_path:
            print(f"  已生成 CBZ: {cbz_path}", flush=True)
        return

    print(f"  找到 {len(image_metas)} 张图片，缺失 {len(missing_metas)} 张", flush=True)
    failed_metas: list[ImageMeta] = []

    def download_metas(metas: list[ImageMeta], retry_round: bool = False) -> list[ImageMeta]:
        failed: list[ImageMeta] = []
        chunks = [metas[index : index + 5] for index in range(0, len(metas), 5)]
        if not chunks:
            return failed

        label = "重试" if retry_round else "下载"
        ticket_session = clone_session(session)

        def request_tickets(chunk: list[ImageMeta]) -> dict[int, ImageResource]:
            print(
                f"  后台申请 ticket: {chunk[0].index}-{chunk[-1].index}/{len(image_metas)}",
                flush=True,
            )
            return get_image_tickets(ticket_session, chunk, base_url=base_url)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future: Future[dict[int, ImageResource]] = executor.submit(request_tickets, chunks[0])
            for chunk_index, chunk in enumerate(chunks):
                next_future: Future[dict[int, ImageResource]] | None = None
                if chunk_index + 1 < len(chunks):
                    next_future = executor.submit(request_tickets, chunks[chunk_index + 1])

                print(
                    f"  等待 ticket 并{label}: {chunk[0].index}-{chunk[-1].index}/{len(image_metas)}",
                    flush=True,
                )
                try:
                    image_urls = future.result()
                except QuotaExceeded:
                    raise
                except Exception as exc:
                    print(f"  获取 ticket 失败: {chunk[0].index}-{chunk[-1].index}/{len(image_metas)}", flush=True)
                    print(f"  原因: {exc}", flush=True)
                    failed.extend(chunk)
                    future = next_future
                    if future is None:
                        break
                    continue

                for meta in chunk:
                    image = image_urls.get(meta.index)
                    if not image:
                        print(f"  获取 ticket 失败 ({meta.index}/{len(image_metas)})", flush=True)
                        failed.append(meta)
                        continue
                    save_path = chapter_dir / f"{meta.index:03d}.jpg"
                    try:
                        download_image(
                            session,
                            image,
                            save_path,
                            base_url=base_url,
                            retries=image_retries,
                            retry_delay=retry_delay,
                            progress=f"({meta.index}/{len(image_metas)})",
                        )
                    except RateLimited as exc:
                        print(f"  下载受限 ({meta.index}/{len(image_metas)}): {exc}", flush=True)
                        failed.append(meta)
                    except requests.RequestException as exc:
                        print(f"  下载失败 ({meta.index}/{len(image_metas)}): {image.url}", flush=True)
                        print(f"  原因: {exc}", flush=True)
                        failed.append(meta)
                    time.sleep(image_delay)

                future = next_future
                if future is None:
                    break
        return failed

    failed_metas = download_metas(missing_metas)
    for retry_round in range(1, 3):
        if not failed_metas:
            break
        print(f"  章节末尾第 {retry_round}/2 轮重试未成功图片: {len(failed_metas)} 张", flush=True)
        failed_metas = download_metas(failed_metas, retry_round=True)

    if failed_metas:
        failed_indexes = ", ".join(str(meta.index) for meta in failed_metas)
        print(f"  以下图片最终仍未成功，未生成 CBZ: {failed_indexes}", flush=True)
        return

    if not is_chapter_complete(chapter_dir, len(image_metas)):
        missing_indexes = [
            str(index)
            for index in range(1, len(image_metas) + 1)
            if existing_image_path(chapter_dir, index) is None
        ]
        print(f"  图片不完整，未生成 CBZ。缺失: {', '.join(missing_indexes)}", flush=True)
        return

    cbz_path = export_chapter_cbz(chapter_dir, expected_images=len(image_metas))
    if cbz_path:
        print(f"  已生成 CBZ: {cbz_path}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Komiic 漫画下载器")
    parser.add_argument("url", nargs="?", help="漫画详情页链接，或配合 --single-chapter 传章节链接")
    parser.add_argument("-o", "--output", default="downloads", help="下载目录")
    parser.add_argument("--export-existing-cbz", default="", help="把已有下载目录中的章节批量导出为 CBZ，不请求 API")
    parser.add_argument("--single-chapter", action="store_true", help="把 URL 当作单个章节下载")
    parser.add_argument("--chapter-ids", default="", help="只下载指定章节 ID，多个 ID 用英文逗号分隔")
    parser.add_argument("--max-chapters", type=int, default=0, help="最多下载多少章，0 表示全部已有章节")
    parser.add_argument("--chapter-delay", type=float, default=5.0, help="章节之间等待秒数")
    parser.add_argument("--image-delay", type=float, default=1.0, help="图片之间等待秒数，遇到 429 时会自动额外冷却")
    parser.add_argument("--image-retries", type=int, default=2, help="单张图片立即尝试次数，默认失败后立即重试 1 次")
    parser.add_argument("--retry-delay", type=float, default=2.0, help="图片重试基础等待秒数")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, choices=ALLOWED_BASE_URLS, help="Komiic 站点地址")
    return parser


def run(args: argparse.Namespace) -> int:
    if args.export_existing_cbz:
        output_dir = Path(args.export_existing_cbz).expanduser().resolve()
        count = export_existing_cbz(output_dir)
        print(f"完成，生成/更新 {count} 个 CBZ 文件", flush=True)
        return 0

    if not args.url:
        print("需要传入漫画链接，或使用 --export-existing-cbz 指定已有下载目录", flush=True)
        return 1

    base_url = normalize_base_url(args.base_url)
    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if base_url == "https://komiic.cc":
        print("已启用国内直连节点: API/Referer 使用 komiic.cc，图片使用 img.komiic.cc", flush=True)
    else:
        print("已启用默认节点: API/Referer 使用 komiic.com，图片使用 img.komiic.com", flush=True)

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    cookie = os.environ.get("KOMIIC_COOKIE", "").strip()
    if cookie:
        session.headers.update({"Cookie": cookie})

    if args.single_chapter:
        chapter_id = parse_chapter_id(args.url)
        if not chapter_id:
            print("单章模式需要传入包含 /chapter/{id}/ 的章节链接", flush=True)
            return 1
        comic_id = parse_comic_id(args.url)
        if comic_id:
            comic_chapters = get_chapters(session, args.url, base_url=base_url)
            matched = [chapter for chapter in comic_chapters if chapter.chapter_id == chapter_id]
            chapters = matched or [
                Chapter(
                    title=safe_name(Path(urlparse(args.url).path).name, fallback=chapter_id),
                    url=f"{base_url}/comic/{comic_id}/chapter/{chapter_id}/images/all",
                    chapter_id=chapter_id,
                    comic_title=get_comic_title(session, comic_id, base_url=base_url),
                    serial=chapter_id,
                    size=0,
                )
            ]
        else:
            title = safe_name(Path(urlparse(args.url).path).name, fallback=chapter_id)
            chapters = [Chapter(title=title, url=args.url, chapter_id=chapter_id, serial=chapter_id)]
    else:
        print("正在读取漫画章节列表...", flush=True)
        chapters = get_chapters(session, args.url, base_url=base_url)

    selected_ids = [item.strip() for item in args.chapter_ids.split(",") if item.strip()]
    if selected_ids and not args.single_chapter:
        chapters_by_id = {chapter.chapter_id: chapter for chapter in chapters}
        chapters = [chapters_by_id[chapter_id] for chapter_id in selected_ids if chapter_id in chapters_by_id]
        missing_ids = [chapter_id for chapter_id in selected_ids if chapter_id not in chapters_by_id]
        if missing_ids:
            print(f"以下章节 ID 不在该漫画章节列表中，已跳过: {', '.join(missing_ids)}", flush=True)
        if not chapters:
            print("没有匹配到可下载章节", flush=True)
            return 1

    if args.max_chapters > 0:
        chapters = chapters[: args.max_chapters]

    print(f"准备下载 {len(chapters)} 个章节", flush=True)
    try:
        for index, chapter in enumerate(chapters, start=1):
            download_chapter(
                session=session,
                chapter=chapter,
                chapter_dir=stable_chapter_dir(output_dir, chapter),
                image_delay=args.image_delay,
                base_url=base_url,
                image_retries=args.image_retries,
                retry_delay=args.retry_delay,
            )
            time.sleep(args.chapter_delay)
    except QuotaExceeded as exc:
        print(f"\n已暂停: {exc}", flush=True)
        print("已下载的文件会保留。配额重置后重新运行同一任务，会自动跳过已存在文件继续下载。", flush=True)
        return QUOTA_EXCEEDED_EXIT_CODE

    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(run(args))
    except KeyboardInterrupt:
        print("\n已取消。", flush=True)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
