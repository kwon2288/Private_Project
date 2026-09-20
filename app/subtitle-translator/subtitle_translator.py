#!/usr/bin/env python3
"""
자막 번역기 - SRT / ASS / SSA / VTT 자막을 한국어로 자동 번역
엔진: Claude, OpenAI(호환 API 포함), DeepL, Google 번역, Ollama(로컬 LLM)

설치: pip install pysubs2 requests charset-normalizer
실행: python subtitle_translator.py
"""
import html
import json
import queue
import re
import threading
import time
from pathlib import Path

import pysubs2
import requests

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:  # GUI 없는 환경에서도 코어 로직은 import 가능하게
    tk = None

try:
    from charset_normalizer import from_bytes as _detect_charset
except ImportError:
    _detect_charset = None

APP_TITLE = "자막 번역기"
CONFIG_PATH = Path.home() / ".subtitle_translator.json"
SUPPORTED_EXT = {".srt": "srt", ".ass": "ass", ".ssa": "ssa", ".vtt": "vtt"}

# ─────────────────────────────────────────────────────────────
# 태그 보호: ASS 오버라이드 태그({\i1} 등), 줄바꿈(\N), 하드 스페이스(\h)
# pysubs2는 SRT/VTT의 <i> 등도 내부적으로 {\i1} 형식으로 변환해 둔다.
# ─────────────────────────────────────────────────────────────
TAG_RE = re.compile(r"(\{[^}]*\}|\\N|\\n|\\h)")
PH_RE = re.compile(r"⟦(\d+)⟧")
LEADING_PH_RE = re.compile(r"^(?:⟦\d+⟧)+")


def protect_tags(text):
    tags = []

    def repl(m):
        tags.append(m.group(0))
        return f"⟦{len(tags) - 1}⟧"

    return TAG_RE.sub(repl, text), tags


def restore_tags(translated, tags, protected_src):
    found = [int(i) for i in PH_RE.findall(translated)]
    if sorted(found) == list(range(len(tags))):
        return PH_RE.sub(lambda m: tags[int(m.group(1))], translated)
    # 플레이스홀더가 깨졌으면: 모두 제거하고 원문 앞쪽 태그만 복원
    clean = re.sub(r"\s{2,}", " ", PH_RE.sub(" ", translated)).strip()
    lead = LEADING_PH_RE.match(protected_src)
    prefix = ""
    if lead:
        prefix = "".join(tags[int(i)] for i in PH_RE.findall(lead.group(0))
                         if tags[int(i)].startswith("{"))
    return prefix + clean


def decode_bytes(raw):
    for enc in ("utf-8-sig", "utf-16"):
        try:
            if enc == "utf-16" and not raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            pass
    if _detect_charset:
        best = _detect_charset(raw).best()
        if best:
            return str(best), best.encoding
    for enc in ("cp949", "cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace"), "utf-8"


# ─────────────────────────────────────────────────────────────
# 번역 엔진
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a professional subtitle translator. Translate subtitle lines into natural, fluent Korean.
Rules:
- Input is a JSON object mapping id -> subtitle line. Return ONLY a JSON object with the SAME ids mapping to the Korean translation. No explanations, no code fences.
- Tokens like ⟦0⟧, ⟦1⟧ are formatting tags or line breaks. Keep every one of them exactly as written and place them at the natural position in the translation.
- Keep translations concise and readable as subtitles.
- Keep speech level (반말/존댓말) consistent with the relationships implied by context.
- Never merge, split, skip or reorder lines. Translate each id independently even if a sentence continues across lines.
- Transliterate names consistently. Follow the glossary if provided."""


def build_user_prompt(batch, prev_lines, context):
    parts = []
    if context.strip():
        parts.append("[작품 정보 / 용어집]\n" + context.strip())
    if prev_lines:
        parts.append("[이전 대사 - 참고만 하고 번역하지 말 것]\n" + "\n".join(prev_lines))
    parts.append("[번역할 대사]\n" + json.dumps(batch, ensure_ascii=False, indent=0))
    return "\n\n".join(parts)


def parse_json_obj(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("응답에서 JSON을 찾지 못함")
    return {str(k): str(v) for k, v in json.loads(text[start:end + 1]).items()}


def http_post(url, headers, payload, timeout=300, retries=4):
    delay = 3
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 529) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            return r.json()
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2


class LLMEngine:
    """JSON 배치 방식 LLM 엔진 공통부"""
    is_llm = True

    def __init__(self, cfg):
        self.cfg = cfg

    def complete(self, system, user):
        raise NotImplementedError

    def translate(self, items, prev_lines, context):
        """items: {id: protected_text} → {id: translated}"""
        result, pending = {}, dict(items)
        for _ in range(3):
            if not pending:
                break
            try:
                out = parse_json_obj(self.complete(SYSTEM_PROMPT, build_user_prompt(pending, prev_lines, context)))
            except ValueError:
                continue
            for k in list(pending):
                if out.get(k, "").strip():
                    result[k] = out[k]
                    del pending[k]
        return result  # 끝내 실패한 줄은 빠짐 → 원문 유지


class ClaudeEngine(LLMEngine):
    def complete(self, system, user):
        data = http_post(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": self.cfg["api_key"], "anthropic-version": "2023-06-01",
             "content-type": "application/json"},
            {"model": self.cfg["model"], "max_tokens": 8192, "system": system,
             "messages": [{"role": "user", "content": user}]})
        return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")


class OpenAIEngine(LLMEngine):
    def complete(self, system, user):
        base = (self.cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        data = http_post(
            f"{base}/chat/completions",
            {"Authorization": f"Bearer {self.cfg['api_key']}", "Content-Type": "application/json"},
            {"model": self.cfg["model"], "response_format": {"type": "json_object"},
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        return data["choices"][0]["message"]["content"]


class OllamaEngine(LLMEngine):
    def complete(self, system, user):
        base = (self.cfg.get("base_url") or "http://localhost:11434").rstrip("/")
        data = http_post(
            f"{base}/api/chat", {"Content-Type": "application/json"},
            {"model": self.cfg["model"], "stream": False, "format": "json",
             "options": {"temperature": 0.3},
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            timeout=900)
        return data["message"]["content"]


class DeepLEngine:
    is_llm = False

    def __init__(self, cfg):
        self.cfg = cfg

    @staticmethod
    def _to_xml(s):
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return PH_RE.sub(lambda m: f'<x i="{m.group(1)}"/>', s)

    @staticmethod
    def _from_xml(s):
        s = re.sub(r'<x i="(\d+)"\s*/>', lambda m: f"⟦{m.group(1)}⟧", s)
        return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

    def translate(self, items, prev_lines, context):
        key = self.cfg["api_key"]
        host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
        ids = list(items)
        payload = {"text": [self._to_xml(items[i]) for i in ids], "target_lang": "KO",
                   "tag_handling": "xml", "ignore_tags": []}
        ctx = "\n".join(filter(None, [context.strip(), "\n".join(prev_lines)]))
        if ctx:
            payload["context"] = ctx
        data = http_post(f"https://{host}/v2/translate",
                         {"Authorization": f"DeepL-Auth-Key {key}", "Content-Type": "application/json"},
                         payload)
        return {i: self._from_xml(t["text"]) for i, t in zip(ids, data["translations"])}


class GoogleEngine:
    """Google Cloud Translation API (v2, Basic) - API 키 방식"""
    is_llm = False
    MAX_Q = 128  # 요청당 최대 문장 수

    def __init__(self, cfg):
        self.cfg = cfg

    @staticmethod
    def _to_html(s):
        s = html.escape(s, quote=False)
        return PH_RE.sub(lambda m: f'<span translate="no">⟦{m.group(1)}⟧</span>', s)

    @staticmethod
    def _from_html(s):
        s = re.sub(r'<span[^>]*>\s*(⟦\d+⟧)\s*</span>', r"\1", s)
        s = re.sub(r"</?span[^>]*>", "", s)
        return html.unescape(s)

    def translate(self, items, prev_lines, context):
        ids, out = list(items), {}
        for b in range(0, len(ids), self.MAX_Q):
            chunk = ids[b:b + self.MAX_Q]
            data = http_post(
                f"https://translation.googleapis.com/language/translate/v2?key={self.cfg['api_key']}",
                {"Content-Type": "application/json"},
                {"q": [self._to_html(items[i]) for i in chunk], "target": "ko", "format": "html"})
            for i, t in zip(chunk, data["data"]["translations"]):
                out[i] = self._from_html(t["translatedText"])
        return out


ENGINES = {
    "Claude": (ClaudeEngine, {"model": "claude-sonnet-5", "base_url": ""}),
    "OpenAI / 호환 API": (OpenAIEngine, {"model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1"}),
    "DeepL": (DeepLEngine, {"model": "", "base_url": ""}),
    "Google 번역": (GoogleEngine, {"model": "", "base_url": ""}),
    "Ollama (로컬)": (OllamaEngine, {"model": "qwen2.5:14b", "base_url": "http://localhost:11434"}),
}


# ─────────────────────────────────────────────────────────────
# 파일 단위 번역
# ─────────────────────────────────────────────────────────────
def translate_file(path, engine, opts, log, progress, stop_event):
    path = Path(path)
    ext = path.suffix.lower()
    text, enc = decode_bytes(path.read_bytes())
    subs = pysubs2.SSAFile.from_string(text)
    log(f"▶ {path.name}  (인코딩: {enc}, 대사 {len(subs)}개)")

    targets = [i for i, ev in enumerate(subs)
               if not ev.is_comment and ev.plaintext.strip() and not re.search(r"\\p[1-9]", ev.text)]
    protected = {str(i): protect_tags(subs[i].text) for i in targets}

    batch_size = max(1, int(opts["batch_size"]))
    done = 0
    for b in range(0, len(targets), batch_size):
        if stop_event.is_set():
            log("  ■ 중지됨")
            return None
        chunk = targets[b:b + batch_size]
        items = {str(i): protected[str(i)][0] for i in chunk}
        prev = [subs[j].plaintext.replace("\n", " ") for j in targets[max(0, b - 5):b]]
        try:
            res = engine.translate(items, prev, opts["context"])
        except Exception as e:
            log(f"  ⚠ 배치 {b // batch_size + 1} 실패, 원문 유지: {e}")
            res = {}
        for i in chunk:
            k = str(i)
            if k not in res:
                continue
            src_prot, tags = protected[k][0], protected[k][1]
            ko = restore_tags(res[k].strip(), tags, src_prot)
            if opts["bilingual"]:  # 원문 줄의 앞쪽 위치/정렬 태그는 중복되지 않게 제거
                orig = re.sub(r"^(?:\{[^}]*\})+", "", subs[i].text)
                subs[i].text = f"{ko}\\N{orig}"
            else:
                subs[i].text = ko
        missing = [i for i in chunk if str(i) not in res]
        if missing and res:
            log(f"  ⚠ {len(missing)}줄 번역 누락 → 원문 유지")
        done += len(chunk)
        progress(done, len(targets))

    out_dir = Path(opts["out_dir"]) if opts["out_dir"] else path.parent
    out_path = out_dir / f"{path.stem}.ko{ext}"
    out_path.write_text(subs.to_string(SUPPORTED_EXT[ext]), encoding=opts["out_encoding"], errors="replace")
    log(f"  ✔ 저장: {out_path}")
    return out_path


# ─────────────────────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────────────────────
class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("820x720")
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.cfg = self.load_config()
        self.cur_engine = None
        self.build_ui()
        self.on_engine_change()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll_queue)

    # ── 설정 ──
    def load_config(self):
        cfg = {"engine": "Claude", "engines": {}, "batch_size": 30, "bilingual": False,
               "out_dir": "", "out_encoding": "utf-8", "context": ""}
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
        for name, (_, defaults) in ENGINES.items():
            cfg["engines"].setdefault(name, {"api_key": "", **defaults})
        return cfg

    def save_config(self):
        self.store_engine_fields()
        self.cfg.update(engine=self.engine_var.get(), batch_size=self.batch_var.get(),
                        bilingual=self.bi_var.get(), out_dir=self.outdir_var.get(),
                        out_encoding=self.enc_var.get(),
                        context=self.ctx_text.get("1.0", "end").strip())
        try:
            CONFIG_PATH.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"설정 저장 실패: {e}")

    # ── UI 구성 ──
    def build_ui(self):
        pad = {"padx": 6, "pady": 4}

        f_files = ttk.LabelFrame(self.root, text="자막 파일 (SRT / ASS / SSA / VTT)")
        f_files.pack(fill="both", expand=True, **pad)
        self.listbox = tk.Listbox(f_files, selectmode="extended", height=8)
        self.listbox.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        bf = ttk.Frame(f_files)
        bf.pack(side="right", fill="y", padx=4)
        for txt, cmd in [("파일 추가", self.add_files), ("폴더 추가", self.add_folder),
                         ("선택 삭제", self.remove_selected), ("모두 삭제", lambda: self.listbox.delete(0, "end"))]:
            ttk.Button(bf, text=txt, command=cmd, width=12).pack(pady=2)

        f_eng = ttk.LabelFrame(self.root, text="번역 엔진")
        f_eng.pack(fill="x", **pad)
        self.engine_var = tk.StringVar(value=self.cfg["engine"])
        ttk.Label(f_eng, text="엔진").grid(row=0, column=0, sticky="w", **pad)
        cb = ttk.Combobox(f_eng, textvariable=self.engine_var, values=list(ENGINES), state="readonly", width=22)
        cb.grid(row=0, column=1, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", lambda e: self.on_engine_change())
        self.key_var, self.model_var, self.url_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(f_eng, text="API 키").grid(row=1, column=0, sticky="w", **pad)
        self.key_entry = ttk.Entry(f_eng, textvariable=self.key_var, show="•", width=60)
        self.key_entry.grid(row=1, column=1, columnspan=3, sticky="we", **pad)
        ttk.Label(f_eng, text="모델").grid(row=2, column=0, sticky="w", **pad)
        self.model_entry = ttk.Entry(f_eng, textvariable=self.model_var, width=28)
        self.model_entry.grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(f_eng, text="Base URL").grid(row=2, column=2, sticky="e", **pad)
        self.url_entry = ttk.Entry(f_eng, textvariable=self.url_var, width=34)
        self.url_entry.grid(row=2, column=3, sticky="we", **pad)
        f_eng.columnconfigure(3, weight=1)

        f_opt = ttk.LabelFrame(self.root, text="옵션")
        f_opt.pack(fill="x", **pad)
        self.batch_var = tk.IntVar(value=self.cfg["batch_size"])
        ttk.Label(f_opt, text="배치 크기(줄)").grid(row=0, column=0, sticky="w", **pad)
        ttk.Spinbox(f_opt, from_=1, to=200, textvariable=self.batch_var, width=6).grid(row=0, column=1, sticky="w", **pad)
        self.bi_var = tk.BooleanVar(value=self.cfg["bilingual"])
        ttk.Checkbutton(f_opt, text="이중 자막 (번역 + 원문)", variable=self.bi_var).grid(row=0, column=2, sticky="w", **pad)
        self.enc_var = tk.StringVar(value=self.cfg["out_encoding"])
        ttk.Label(f_opt, text="출력 인코딩").grid(row=0, column=3, sticky="e", **pad)
        ttk.Combobox(f_opt, textvariable=self.enc_var, values=["utf-8", "utf-8-sig", "cp949"],
                     state="readonly", width=10).grid(row=0, column=4, sticky="w", **pad)
        self.outdir_var = tk.StringVar(value=self.cfg["out_dir"])
        ttk.Label(f_opt, text="출력 폴더").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f_opt, textvariable=self.outdir_var).grid(row=1, column=1, columnspan=3, sticky="we", **pad)
        ttk.Button(f_opt, text="찾기", command=self.pick_outdir).grid(row=1, column=4, sticky="w", **pad)
        ttk.Label(f_opt, text="(비우면 원본과 같은 폴더에 *.ko.srt 형태로 저장)",
                  foreground="gray").grid(row=2, column=1, columnspan=4, sticky="w", padx=6)
        ttk.Label(f_opt, text="작품 정보 /\n용어집").grid(row=3, column=0, sticky="nw", **pad)
        self.ctx_text = tk.Text(f_opt, height=3, wrap="word")
        self.ctx_text.grid(row=3, column=1, columnspan=4, sticky="we", **pad)
        self.ctx_text.insert("1.0", self.cfg["context"])
        f_opt.columnconfigure(3, weight=1)

        f_run = ttk.Frame(self.root)
        f_run.pack(fill="x", **pad)
        self.start_btn = ttk.Button(f_run, text="번역 시작", command=self.start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(f_run, text="중지", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.prog = ttk.Progressbar(f_run, mode="determinate")
        self.prog.pack(side="left", fill="x", expand=True, padx=8)
        self.prog_label = ttk.Label(f_run, text="대기 중", width=22)
        self.prog_label.pack(side="left")

        self.log_text = tk.Text(self.root, height=10, state="disabled", bg="#1e1e1e", fg="#d4d4d4")
        self.log_text.pack(fill="both", expand=True, **pad)

    # ── 엔진 필드 ──
    def store_engine_fields(self):
        if self.cur_engine:
            self.cfg["engines"][self.cur_engine].update(
                api_key=self.key_var.get().strip(), model=self.model_var.get().strip(),
                base_url=self.url_var.get().strip())

    def on_engine_change(self):
        self.store_engine_fields()
        name = self.engine_var.get()
        ec = self.cfg["engines"][name]
        self.key_var.set(ec.get("api_key", ""))
        self.model_var.set(ec.get("model", ""))
        self.url_var.set(ec.get("base_url", ""))
        self.key_entry.config(state="disabled" if name.startswith("Ollama") else "normal")
        self.model_entry.config(state="disabled" if name in ("DeepL", "Google 번역") else "normal")
        self.url_entry.config(state="normal" if name.startswith(("OpenAI", "Ollama")) else "disabled")
        self.cur_engine = name

    # ── 파일 목록 ──
    def _add_path(self, p):
        p = str(p)
        if Path(p).suffix.lower() in SUPPORTED_EXT and ".ko." not in Path(p).name.lower() \
                and p not in self.listbox.get(0, "end"):
            self.listbox.insert("end", p)

    def add_files(self):
        for p in filedialog.askopenfilenames(filetypes=[("자막", "*.srt *.ass *.ssa *.vtt"), ("모든 파일", "*.*")]):
            self._add_path(p)

    def add_folder(self):
        d = filedialog.askdirectory()
        if d:
            for p in sorted(Path(d).rglob("*")):
                self._add_path(p)

    def remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)

    def pick_outdir(self):
        d = filedialog.askdirectory()
        if d:
            self.outdir_var.set(d)

    # ── 실행 ──
    def start(self):
        files = list(self.listbox.get(0, "end"))
        if not files:
            messagebox.showwarning(APP_TITLE, "번역할 자막 파일을 추가하세요.")
            return
        self.save_config()
        name = self.engine_var.get()
        ec = self.cfg["engines"][name]
        if not name.startswith("Ollama") and not ec["api_key"]:
            messagebox.showwarning(APP_TITLE, "API 키를 입력하세요.")
            return
        engine = ENGINES[name][0](ec)
        opts = {"batch_size": self.batch_var.get(), "bilingual": self.bi_var.get(),
                "out_dir": self.outdir_var.get().strip(), "out_encoding": self.enc_var.get(),
                "context": self.ctx_text.get("1.0", "end")}
        self.stop_event.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.worker = threading.Thread(target=self.run_jobs, args=(files, engine, opts), daemon=True)
        self.worker.start()

    def run_jobs(self, files, engine, opts):
        ok = 0
        for n, f in enumerate(files, 1):
            if self.stop_event.is_set():
                break
            self.q.put(("file", f"{n}/{len(files)}"))
            try:
                if translate_file(f, engine, opts,
                                  log=lambda m: self.q.put(("log", m)),
                                  progress=lambda d, t: self.q.put(("prog", (d, t))),
                                  stop_event=self.stop_event):
                    ok += 1
            except Exception as e:
                self.q.put(("log", f"  ✖ 실패: {Path(f).name} - {e}"))
        self.q.put(("done", f"완료: {ok}/{len(files)}개 파일"))

    def stop(self):
        self.stop_event.set()
        self.log("중지 요청됨 (현재 배치 끝나면 멈춤)")

    def poll_queue(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log(val)
                elif kind == "file":
                    self.cur_file = val
                elif kind == "prog":
                    d, t = val
                    self.prog["maximum"] = max(t, 1)
                    self.prog["value"] = d
                    self.prog_label.config(text=f"파일 {getattr(self, 'cur_file', '')}  {d}/{t}줄")
                elif kind == "done":
                    self.log(val)
                    self.prog_label.config(text=val)
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    def log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def on_close(self):
        self.stop_event.set()
        self.save_config()
        self.root.destroy()


if __name__ == "__main__":
    if tk is None:
        raise SystemExit("tkinter가 설치되어 있지 않습니다.")
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()
