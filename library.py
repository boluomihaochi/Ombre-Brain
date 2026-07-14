# ============================================================
# Module: Ombre Library (library.py)
# 模块：听澍书斋 —— 共读系统 + 学习文件系统
#
# 设计原则：
# - 最小侵入：server.py 只需两行代码引入本模块
# - 数据全部存在 buckets/.library/ 下（Render 持久盘内，重启不丢）
# - 上传走 HTTP（复用 dashboard 的 cookie 认证），书的内容不经过对话窗口
# - MCP 工具只做"按章按段读取"，每段 ≤4500 字，避开 MCP 返回截断
#
# 目录结构：
#   buckets/.library/
#     books/<书名>/meta.json            书籍元数据 + 章节目录
#     books/<书名>/ch_0001_p01.txt      第1章第1段（已转UTF-8）
#     files/<文件名>.txt                学习文件（现用现取）
#     notes/<书名>.txt                  读书笔记（追加式，带时间戳）
#
# 接入方式（server.py 中、_require_auth 定义之后加两行）：
#   import library as _ombre_library
#   _ombre_library.register(mcp, config, _require_auth)
# ============================================================

import io
import os
import re
import json
import time
import logging
import datetime

logger = logging.getLogger("ombre_brain.library")

# 每段最大字符数。中文 1 字≈3 字节 UTF-8，4500 字≈13.5KB，
# 低于实测约 14KB 的 MCP 单次返回截断阈值，留有余量。
MAX_PART_CHARS = 4500
MAX_UPLOAD_BYTES = 40 * 1024 * 1024  # 上传上限 40MB

_HERE = os.path.dirname(os.path.abspath(__file__))

# --- 章节标题识别 ---
# 匹配行首的：第X章/回/节/卷/集/部/篇/幕（中文或阿拉伯数字）、
# Chapter/Part + 编号、以及 序章/序言/楔子/引子/尾声/终章/后记/番外 等关键词。
# 仅匹配较短的独立行（≤40字），避免误伤正文里出现的"第三章"字样。
_CHAPTER_RE = re.compile(
    r"^(?:"
    r"第\s*[0-9０-９零〇一二三四五六七八九十百千万两]+\s*[章回节卷集部篇幕]"
    r"|(?:Chapter|CHAPTER|Chap\.?|Part|PART)\s*[0-9IVXLCivxlc]+"
    r"|序章|序言|自序|楔子|引子|尾声|终章|后记|跋|番外"
    r")(?:[\s:：.、－—-].{0,30})?$"
)


# ============================================================
# 基础工具函数
# ============================================================

def _lib_root(config) -> str:
    """书斋根目录：默认 buckets/.library（在 Render 持久盘内）。
    可用环境变量 OMBRE_LIBRARY_DIR 覆盖。"""
    root = os.environ.get("OMBRE_LIBRARY_DIR", "").strip() or os.path.join(
        config["buckets_dir"], ".library"
    )
    for sub in ("books", "files", "notes"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def _safe_name(name: str) -> str:
    """清洗文件/书名：去路径、去非法字符、限长，防目录穿越。"""
    name = os.path.basename((name or "").strip())
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)
    name = name.strip(". ")
    return name[:80] or f"未命名_{int(time.time())}"


def _strip_ext(name: str) -> str:
    base, _ = os.path.splitext(name)
    return base or name


def _decode_bytes(data: bytes) -> str:
    """自动识别编码并转为 str：UTF-8 / UTF-8-BOM / UTF-16 / GB18030(含GBK)。"""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    if data[:3] == b"\xef\xbb\xbf":
        return data.decode("utf-8-sig")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _pdf_to_text(data: bytes) -> str:
    """从文字版 PDF 抽取纯文本。扫描版（图片型）PDF 抽不出字会报错。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("缺少 pypdf 依赖：请确认 requirements.txt 已加入 pypdf 并重新部署")
    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "") for page in reader.pages]
    text = "\n".join(pages)
    if len(re.sub(r"\s", "", text)) < 200:
        raise RuntimeError("这个 PDF 几乎抽不出文字，可能是扫描版（图片型），换文字版或 txt 吧")
    return text


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _split_chapters(text: str):
    """按章节标题拆分，返回 [(title, body), ...]。
    识别不到 2 个以上章节标记时，退化为按 1.2 万字定长分块。"""
    lines = text.split("\n")
    marks = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s and len(s) <= 40 and _CHAPTER_RE.match(s):
            marks.append((i, s))

    chapters = []
    if len(marks) >= 2:
        if marks[0][0] > 0:
            head = "\n".join(lines[: marks[0][0]]).strip()
            if len(head) > 80:  # 卷首语/简介等
                chapters.append(("卷首", head))
        for j, (idx, title) in enumerate(marks):
            end = marks[j + 1][0] if j + 1 < len(marks) else len(lines)
            body = "\n".join(lines[idx:end]).strip()
            if body:
                chapters.append((title, body))
    else:
        whole = text.strip()
        size = 12000
        n = max(1, (len(whole) + size - 1) // size)
        for k in range(n):
            chapters.append((f"第{k + 1}部分", whole[k * size : (k + 1) * size]))
    return chapters


def _split_parts(body: str):
    """把一章按 MAX_PART_CHARS 切成若干段，尽量在段落边界断开。"""
    if len(body) <= MAX_PART_CHARS:
        return [body]
    paras = body.split("\n")
    parts, cur = [], ""
    for p in paras:
        candidate = (cur + "\n" + p) if cur else p
        if cur and len(candidate) > MAX_PART_CHARS:
            parts.append(cur)
            cur = p
        else:
            cur = candidate
        while len(cur) > MAX_PART_CHARS:  # 单个超长段落硬切
            parts.append(cur[:MAX_PART_CHARS])
            cur = cur[MAX_PART_CHARS:]
    if cur.strip():
        parts.append(cur)
    return parts


# ============================================================
# 书籍入库 / 读取
# ============================================================

def _book_dir(root: str, book: str) -> str:
    return os.path.join(root, "books", _safe_name(book))


def _load_meta(root: str, book: str):
    path = os.path.join(_book_dir(root, book), "meta.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _ingest_book(root: str, title: str, text: str) -> dict:
    """转码后的全文 → 拆章 → 切段 → 落盘 → 写 meta.json。返回 meta。"""
    title = _strip_ext(_safe_name(title))
    text = _normalize_text(text)
    bdir = _book_dir(root, title)
    os.makedirs(bdir, exist_ok=True)
    # 清掉旧章节文件（重复上传同名书 = 覆盖）
    for fn in os.listdir(bdir):
        if fn.startswith("ch_") and fn.endswith(".txt"):
            os.remove(os.path.join(bdir, fn))

    chapters_meta = []
    for ci, (ctitle, body) in enumerate(_split_chapters(text), start=1):
        parts = _split_parts(body)
        for pi, part in enumerate(parts, start=1):
            fn = f"ch_{ci:04d}_p{pi:02d}.txt"
            with open(os.path.join(bdir, fn), "w", encoding="utf-8") as f:
                f.write(part)
        chapters_meta.append({"title": ctitle, "parts": len(parts), "chars": len(body)})

    meta = {
        "title": title,
        "added": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_chars": len(text),
        "chapters": chapters_meta,
    }
    with open(os.path.join(bdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return meta


def _note_path(root: str, book: str) -> str:
    return os.path.join(root, "notes", _safe_name(book) + ".txt")


# ============================================================
# 注册入口：MCP 工具 + HTTP 路由
# ============================================================

def register(mcp, config, require_auth):
    """在 server.py 中调用：注册全部书斋工具和路由。
    require_auth: server.py 的 _require_auth（未登录返回401响应，否则返回None）。"""
    from starlette.responses import JSONResponse, HTMLResponse, PlainTextResponse

    root = _lib_root(config)
    logger.info(f"Ombre Library mounted at {root}")

    # --------------------------------------------------------
    # MCP 工具（描述按关键词召唤优化：含中英同义词 + 参数名）
    # --------------------------------------------------------

    @mcp.tool()
    async def list_books() -> str:
        """【Ombre Library 听澍书斋 共读系统 reading bookshelf】列出书架上所有书：书名、章节数、总字数、是否有笔记。相关工具: book_info read_chapter save_note read_note。"""
        books_dir = os.path.join(root, "books")
        names = sorted(os.listdir(books_dir)) if os.path.exists(books_dir) else []
        rows = []
        for name in names:
            meta = _load_meta(root, name)
            if not meta:
                continue
            has_note = "📒有笔记" if os.path.exists(_note_path(root, name)) else "无笔记"
            rows.append(
                f"《{meta['title']}》 {len(meta['chapters'])}章 "
                f"{meta['total_chars']}字 {has_note} ({meta['added']}入库)"
            )
        return "📚 书架：\n" + "\n".join(rows) if rows else "书架还空着，去 /library 传一本吧。"

    @mcp.tool()
    async def book_info(book: str) -> str:
        """【Ombre Library 听澍书斋 共读系统 目录 TOC chapters】查看某本书的章节目录：每章标题、段数(parts)、字数。参数 book=书名。读正文用 read_chapter(book, chapter, part)。"""
        meta = _load_meta(root, book)
        if not meta:
            return f"书架上没有《{_safe_name(book)}》。先 list_books 看看有什么。"
        rows = [
            f"{i}. {c['title']}  ({c['chars']}字"
            + (f", {c['parts']}段" if c["parts"] > 1 else "")
            + ")"
            for i, c in enumerate(meta["chapters"], start=1)
        ]
        return (
            f"《{meta['title']}》 共{len(meta['chapters'])}章 {meta['total_chars']}字\n"
            + "\n".join(rows)
        )

    @mcp.tool()
    async def read_chapter(book: str, chapter: int, part: int = 1) -> str:
        """【Ombre Library 听澍书斋 共读系统 读书 read book chapter part】读取某书某章正文。book=书名, chapter=章序号(从1起, 见book_info), part=段序号(从1起,默认1)。每段≤4500字；末尾会提示是否还有下一段/下一章。"""
        meta = _load_meta(root, book)
        if not meta:
            return f"书架上没有《{_safe_name(book)}》。"
        chs = meta["chapters"]
        if not (1 <= chapter <= len(chs)):
            return f"《{meta['title']}》只有 {len(chs)} 章，没有第 {chapter} 章。"
        cinfo = chs[chapter - 1]
        if not (1 <= part <= cinfo["parts"]):
            return f"「{cinfo['title']}」只有 {cinfo['parts']} 段，没有第 {part} 段。"

        path = os.path.join(_book_dir(root, meta["title"]), f"ch_{chapter:04d}_p{part:02d}.txt")
        if not os.path.exists(path):
            return "章节文件缺失，可能需要重新上传这本书。"
        with open(path, "r", encoding="utf-8") as f:
            body = f.read()

        header = f"《{meta['title']}》第{chapter}/{len(chs)}章「{cinfo['title']}」"
        if cinfo["parts"] > 1:
            header += f" 第{part}/{cinfo['parts']}段"
        if part < cinfo["parts"]:
            footer = f"\n\n—— 本章未完，下一段: read_chapter(\"{meta['title']}\", {chapter}, {part + 1})"
        elif chapter < len(chs):
            footer = f"\n\n—— 本章完。下一章: 「{chs[chapter]['title']}」 read_chapter(\"{meta['title']}\", {chapter + 1})"
        else:
            footer = "\n\n—— 全书完 🌧️"
        return f"{header}\n{'─' * 24}\n{body}{footer}"

    @mcp.tool()
    async def save_note(book: str, content: str) -> str:
        """【Ombre Library 听澍书斋 共读系统 读书笔记 notes save】给某本书追加一条读书笔记(自动带时间戳)，存持久盘。换窗口续读时先 read_note 恢复上下文。book=书名, content=笔记正文(markdown)。"""
        if not content or not content.strip():
            return "笔记内容为空。"
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n\n## {stamp}\n{content.strip()}\n"
        with open(_note_path(root, book), "a", encoding="utf-8") as f:
            f.write(entry)
        return f"📒 已记入《{_safe_name(book)}》的笔记 ({stamp})"

    @mcp.tool()
    async def read_note(book: str) -> str:
        """【Ombre Library 听澍书斋 共读系统 读书笔记 notes read 恢复上下文】读取某本书的全部读书笔记。换窗口续读第一步就调这个。book=书名。"""
        path = _note_path(root, book)
        if not os.path.exists(path):
            return f"《{_safe_name(book)}》还没有笔记。"
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if len(text) > 18000:  # 笔记过长时保留最近部分
            text = "…(更早的笔记已省略)…\n" + text[-18000:]
        return f"📒《{_safe_name(book)}》读书笔记：\n{text}"

    @mcp.tool()
    async def delete_book(book: str) -> str:
        """【Ombre Library 听澍书斋 共读系统 删除书籍 delete book】把一本书从书架删除(笔记保留)。book=书名。不可恢复，删前先和小诺确认。"""
        bdir = _book_dir(root, book)
        if not os.path.exists(bdir):
            return f"书架上没有《{_safe_name(book)}》。"
        for fn in os.listdir(bdir):
            os.remove(os.path.join(bdir, fn))
        os.rmdir(bdir)
        return f"已把《{_safe_name(book)}》从书架取下(笔记仍保留)。"

    @mcp.tool()
    async def list_files() -> str:
        """【Ombre Library 学习文件系统 study files 课件 PPT资料】列出学习文件区的所有文件：文件名、字数。读取用 read_file(name, part)。"""
        fdir = os.path.join(root, "files")
        names = sorted(fn for fn in os.listdir(fdir) if fn.endswith(".txt")) if os.path.exists(fdir) else []
        rows = []
        for fn in names:
            size = os.path.getsize(os.path.join(fdir, fn))
            rows.append(f"{_strip_ext(fn)} (~{size // 3}字)")
        return "🗂 学习文件：\n" + "\n".join(rows) if rows else "学习文件区是空的，去 /library 上传。"

    @mcp.tool()
    async def read_file(name: str, part: int = 1) -> str:
        """【Ombre Library 学习文件系统 study files read 现用现取】分段读取学习文件。name=文件名(见list_files), part=段序号(从1起)。每段≤4500字，末尾提示余量。"""
        path = os.path.join(root, "files", _safe_name(name) + ".txt")
        if not os.path.exists(path):
            return f"学习文件区没有「{_safe_name(name)}」。先 list_files 看看。"
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        parts = _split_parts(text)
        if not (1 <= part <= len(parts)):
            return f"「{_safe_name(name)}」只有 {len(parts)} 段。"
        footer = (
            f"\n\n—— 未完，下一段: read_file(\"{_safe_name(name)}\", {part + 1})"
            if part < len(parts)
            else "\n\n—— 文件读完。"
        )
        return f"🗂「{_safe_name(name)}」第{part}/{len(parts)}段\n{'─' * 24}\n{parts[part - 1]}{footer}"

    @mcp.tool()
    async def delete_file(name: str) -> str:
        """【Ombre Library 学习文件系统 删除文件 delete file】删除一个学习文件。name=文件名。不可恢复，删前先和小诺确认。"""
        path = os.path.join(root, "files", _safe_name(name) + ".txt")
        if not os.path.exists(path):
            return f"学习文件区没有「{_safe_name(name)}」。"
        os.remove(path)
        return f"已删除学习文件「{_safe_name(name)}」。"

    # --------------------------------------------------------
    # HTTP 路由（上传页 + API，复用 dashboard 认证）
    # --------------------------------------------------------

    @mcp.custom_route("/library", methods=["GET"])
    async def library_page(request):
        page = os.path.join(_HERE, "library.html")
        if not os.path.exists(page):
            return PlainTextResponse("library.html 缺失：请确认它已和 library.py 一起提交到仓库", status_code=500)
        with open(page, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())

    @mcp.custom_route("/api/library/list", methods=["GET"])
    async def api_library_list(request):
        err = require_auth(request)
        if err:
            return err
        books = []
        bdir = os.path.join(root, "books")
        for name in sorted(os.listdir(bdir)) if os.path.exists(bdir) else []:
            meta = _load_meta(root, name)
            if meta:
                books.append({
                    "title": meta["title"],
                    "chapters": len(meta["chapters"]),
                    "chars": meta["total_chars"],
                    "added": meta["added"],
                    "has_note": os.path.exists(_note_path(root, meta["title"])),
                })
        files = []
        fdir = os.path.join(root, "files")
        for fn in sorted(os.listdir(fdir)) if os.path.exists(fdir) else []:
            if fn.endswith(".txt"):
                files.append({"name": _strip_ext(fn), "bytes": os.path.getsize(os.path.join(fdir, fn))})
        return JSONResponse({"books": books, "files": files})

    @mcp.custom_route("/api/library/upload", methods=["POST"])
    async def api_library_upload(request):
        err = require_auth(request)
        if err:
            return err
        kind = request.query_params.get("type", "book")
        raw_name = request.query_params.get("name", "")
        data = await request.body()
        if not data:
            return JSONResponse({"error": "没收到文件内容"}, status_code=400)
        if len(data) > MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "文件超过 40MB 上限"}, status_code=400)

        try:
            if raw_name.lower().endswith(".pdf") or data[:5] == b"%PDF-":
                text = _pdf_to_text(data)
            else:
                text = _decode_bytes(data)
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.warning(f"Library upload decode failed: {e}")
            return JSONResponse({"error": f"解析失败: {e}"}, status_code=400)

        title = _strip_ext(_safe_name(raw_name))
        try:
            if kind == "book":
                meta = _ingest_book(root, title, text)
                return JSONResponse({
                    "ok": True, "kind": "book", "title": meta["title"],
                    "chapters": len(meta["chapters"]), "chars": meta["total_chars"],
                    "toc_preview": [c["title"] for c in meta["chapters"][:8]],
                })
            else:
                path = os.path.join(root, "files", title + ".txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(_normalize_text(text))
                return JSONResponse({"ok": True, "kind": "file", "title": title, "chars": len(text)})
        except Exception as e:
            logger.error(f"Library ingest failed: {e}")
            return JSONResponse({"error": f"入库失败: {e}"}, status_code=500)

    @mcp.custom_route("/api/library/delete", methods=["POST"])
    async def api_library_delete(request):
        err = require_auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        kind, name = body.get("type"), _safe_name(body.get("name", ""))
        if kind == "book":
            bdir = _book_dir(root, name)
            if os.path.exists(bdir):
                for fn in os.listdir(bdir):
                    os.remove(os.path.join(bdir, fn))
                os.rmdir(bdir)
                return JSONResponse({"ok": True})
        elif kind == "file":
            path = os.path.join(root, "files", name + ".txt")
            if os.path.exists(path):
                os.remove(path)
                return JSONResponse({"ok": True})
        return JSONResponse({"error": "没找到要删除的对象"}, status_code=404)

    @mcp.custom_route("/api/library/note", methods=["GET"])
    async def api_library_note(request):
        err = require_auth(request)
        if err:
            return err
        name = _safe_name(request.query_params.get("name", ""))
        path = _note_path(root, name)
        if not os.path.exists(path):
            return JSONResponse({"error": "没有笔记"}, status_code=404)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return JSONResponse({"content": content})

    logger.info("Ombre Library: 9 tools + 5 routes registered")
