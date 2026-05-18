"""LTX-2.3 Director UI — Gradio panel for storyboard-driven local video generation.

啟動：bash ui/run.sh
依賴：ltx-2-mlx 已安裝（~/ltx-2-mlx/.venv），ffmpeg 在 PATH。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import gradio as gr
import pandas as pd

REPO_DIR = Path.home() / "ltx-2-mlx"
OUT_DIR = REPO_DIR / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)
LTX_BIN = REPO_DIR / ".venv" / "bin" / "ltx-2-mlx"

ASPECT_WH = {
    "16:9": (704, 384),
    "9:16": (384, 704),
    "1:1": (512, 512),
    "21:9": (704, 288),
    "4:5": (480, 608),
}

MODE_FLAGS = {
    "fast (distilled, 約 1 分鐘)": "--distilled",
    "hq (two-stages-hq, 約 5+ 分鐘)": "--two-stages-hq",
}

I2V_MODES = {
    "ic-lora + canny（推薦，最強保留，最快）": "ic-lora-canny",
    "雙錨 two-stage（既有，中等保留）": "two-stage-dual-anchor",
}


def duration_to_frames(seconds: float, fps: int) -> int:
    """對齊到 1+8N，LTX-2 latent 時序壓縮要求。"""
    target = int(seconds * fps)
    n = max(1, round((target - 1) / 8))
    return 1 + n * 8


TTS_VOICES_ZH_TW = ["Meijia", "Sandy", "Eddy", "Flo", "Rocko",
                    "Grandma", "Grandpa", "Reed"]

DEFAULT_NARRATION_FONT = "/System/Library/Fonts/PingFang.ttc"


def make_initial_df() -> pd.DataFrame:
    """4 欄分鏡表：秒 / Prompt（畫面動作）/ i2v 圖 / 旁白（TTS+字幕用）。"""
    return pd.DataFrame(
        [
            [3, "廣角航拍：海邊夕陽，金色波光", "",
             "在台灣東部的某個小漁村，住著一位八十歲的老漁夫。"],
            [5, "推近：礁石上的海鳥拍翅起飛，慢動作", "",
             "每天清晨，他會踏上熟悉的礁石小路，等待海鳥啟程的瞬間。"],
            [2, "特寫：浪花拍上岩石碎成水霧", "",
             "據說那一刻，整片海洋會跟著呼吸。"],
        ],
        columns=["秒", "Prompt", "i2v 圖片路徑(選填)", "旁白文字(選填)"],
    )


def get_audio_duration(path: Path) -> float:
    """用 ffprobe 取得音檔秒數；失敗回 0.0。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def adjust_duration_to_narration(seconds: float, narration: str,
                                  voice: str, tmpdir: Path,
                                  base_name: str) -> tuple[float, Path | None]:
    """先跑 TTS 取得實際旁白秒數，若超過 seconds 則延長。

    回傳 (調整後秒數對齊到 1+8N 幀後的秒數, 預生 aiff 路徑)。
    若沒旁白或 TTS 失敗，回傳 (原秒數, None)。
    """
    import math
    if not narration or not narration.strip():
        return seconds, None
    aiff = tmpdir / f"{base_name}.aiff"
    if not tts_to_aiff(narration, voice, aiff):
        return seconds, None
    dur = get_audio_duration(aiff)
    if dur <= 0:
        return seconds, aiff
    # 旁白比預設長 → 延長到 ceil(dur + 0.3) 秒（多 0.3 秒緩衝）
    if dur > seconds:
        return math.ceil(dur + 0.3), aiff
    return seconds, aiff


def tts_to_aiff(text: str, voice: str, out_path: Path) -> bool:
    """用 macOS `say` 把文字合成為 AIFF。回傳是否成功。"""
    if not text or not text.strip():
        return False
    try:
        subprocess.run(
            ["say", "-v", voice, "-o", str(out_path), text.strip()],
            check=True, capture_output=True, text=True, timeout=60,
        )
        return out_path.exists() and out_path.stat().st_size > 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def write_srt(text: str, duration: float, srt_path: Path) -> None:
    """單行 SRT，整段 0..duration 顯示。"""
    def fmt(t: float) -> str:
        h = int(t // 3600); m = int((t % 3600) // 60)
        s = int(t % 60); ms = int((t - int(t)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    srt_path.write_text(
        f"1\n{fmt(0)} --> {fmt(duration)}\n{text.strip()}\n",
        encoding="utf-8",
    )


def mux_audio_subtitle(video_in: Path, audio_in: Path | None,
                       srt_in: Path | None, video_out: Path) -> tuple[bool, str]:
    """把 audio 與字幕合進影片。字幕用 PingFang.ttc 顯示中文。"""
    vf = []
    if srt_in is not None and srt_in.exists():
        # subtitles 濾鏡需要 ASS-style font 設定；force_style 加 PingFang
        force_style = "FontName=PingFang TC,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=1,MarginV=24"
        vf.append(f"subtitles='{srt_in}':force_style='{force_style}'")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video_in)]
    if audio_in is not None and audio_in.exists():
        cmd += ["-i", str(audio_in)]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if audio_in is not None and audio_in.exists():
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest"]
    else:
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    cmd += [str(video_out)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if video_out.exists() and video_out.stat().st_size > 0:
        return True, ""
    return False, (proc.stderr or "")[-300:]


def post_process_shot(shot_video: Path, narration: str, duration: float,
                       voice: str, burn_subtitle: bool,
                       pre_audio: Path | None = None) -> tuple[Path, str]:
    """為單鏡頭做 TTS + 字幕後處理。

    pre_audio 為預先產生的 TTS aiff（如已在 pipeline 中先跑過以取得長度）。
    若提供且存在，跳過 TTS 直接複用。
    """
    if not narration or not narration.strip():
        return shot_video, "(無旁白，跳過後處理)"

    tmpdir = shot_video.parent
    base = shot_video.stem
    aiff = pre_audio if (pre_audio and pre_audio.exists()) else tmpdir / f"{base}.aiff"
    srt = tmpdir / f"{base}.srt"
    final = tmpdir / f"{base}_av.mp4"

    msgs = []
    if pre_audio and pre_audio.exists():
        audio_ok = True
        msgs.append(f"TTS({voice}, 預生)")
    else:
        audio_ok = tts_to_aiff(narration, voice, aiff)
        msgs.append(f"TTS({voice})" if audio_ok else "TTS 失敗")

    if burn_subtitle:
        write_srt(narration, duration, srt)
        msgs.append("字幕")

    ok, err = mux_audio_subtitle(
        shot_video,
        aiff if audio_ok else None,
        srt if burn_subtitle else None,
        final,
    )
    if ok:
        return final, " + ".join(msgs) + " 已套用"
    return shot_video, f"後處理失敗: {err}"


# ============================================================
# AI 靜圖生成（mflux subprocess）
# ============================================================

FLUX_MODELS = {
    "qwen (Qwen-Image，開放，推薦先試)": ("qwen", 8),
    "fibo-lite (Hailuo Fibo Lite，開放)": ("fibo-lite", 8),
    "schnell (FLUX.1-schnell，需 HF 接受授權)": ("schnell", 4),
    "dev (FLUX.1-dev，需 HF 接受授權)": ("dev", 25),
    "z-image-turbo（mflux 整合 BUG，慎用）": ("z-image-turbo", 8),
    "z-image（mflux 整合 BUG，慎用）": ("z-image", 30),
}


def build_flux_cmd(prompt: str, model_key: str, aspect: str,
                   seed: int, out_path: Path) -> list[str]:
    """構建 mflux-generate 命令。

    FLUX.1 系列是 gated repo（匿名 401）；若選 schnell/dev，需先：
    1. https://huggingface.co/black-forest-labs/FLUX.1-schnell 同意授權
    2. huggingface-cli login（或設 HF_TOKEN env var）
    z-image / z-image-turbo 為開放權重，不需登入。
    """
    model, steps = FLUX_MODELS[model_key]
    w, h = ASPECT_WH[aspect]
    cmd = [
        "uv", "run", "--with", "mflux",
        "mflux-generate",
        "--prompt", prompt,
        "--model", model,
        "--steps", str(steps),
        "--seed", str(int(seed)) if int(seed) >= 0 else "0",
        "--width", str(w), "--height", str(h),
        "--low-ram",
        "--output", str(out_path),
    ]
    # FLUX 系列支援 q4 量化以省記憶體
    if model in ("schnell", "dev"):
        cmd += ["--quantize", "4"]
    return cmd


def _flux_error_hint(log_text: str) -> str:
    """從 log 偵測常見錯誤並回傳可執行的提示。"""
    if "401" in log_text or "GatedRepoError" in log_text or "Unauthorized" in log_text:
        return (
            "\n        提示：FLUX.1 是 gated repo。請改選「z-image-turbo（開放）」，"
            "或先到 https://huggingface.co/black-forest-labs/FLUX.1-schnell "
            "接受授權，然後跑 `huggingface-cli login`（會把 token 存到 "
            "~/.cache/huggingface/token），重啟 UI 即可。"
        )
    return ""


def stream_flux(prompt: str, model_key: str, count: int, aspect: str,
                seed: int, progress=gr.Progress()):
    """生成 count 張靜圖，串流回 UI。"""
    if not prompt or not prompt.strip():
        yield "請輸入 prompt", [], "請輸入 prompt"
        return

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"flux-{timestamp}"
    log_lines = [f"開始生成 {count} 張圖（{model_key}, {aspect}）"]
    yield "\n".join(log_lines), [], "生成中..."

    produced: list[str] = []
    for i in range(int(count)):
        s = int(seed) if int(seed) >= 0 else (int(seed) - i) if seed != -1 else -1
        if int(seed) == -1:
            actual_seed = int(datetime.now().timestamp()) + i
        else:
            actual_seed = int(seed) + i
        out_path = OUT_DIR / f"{base}_{i+1:02d}.png"
        cmd = build_flux_cmd(prompt, model_key, aspect, actual_seed, out_path)
        progress(i / max(1, int(count)), desc=f"Image {i+1}/{count}")
        log_lines.append(f"\n=== 圖 {i+1}/{count} | seed={actual_seed} ===")
        yield "\n".join(log_lines[-30:]), produced, "生成中..."

        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert p.stdout is not None
        for raw in p.stdout:
            for line in raw.replace("\r", "\n").split("\n"):
                line = line.strip()
                if not line:
                    continue
                if any(k in line for k in ("Generating", "Downloading", "Fetching",
                                            "step", "Step", "Saved", "Error",
                                            "Traceback", "%")):
                    log_lines.append(f"   {line[-180:]}")
                    yield "\n".join(log_lines[-30:]), produced, "生成中..."
        p.wait()
        if p.returncode == 0 and out_path.exists():
            produced.append(str(out_path))
            log_lines.append(f"   -> 完成: {out_path.name}")
        else:
            hint = _flux_error_hint("\n".join(log_lines[-12:]))
            log_lines.append(f"   -> 失敗 exit={p.returncode}{hint}")
        yield "\n".join(log_lines[-30:]), produced, f"已產出 {len(produced)} 張"

    progress(1.0, desc="完成")
    log_lines.append(f"\n全部完成。{len(produced)} / {count} 張")
    yield "\n".join(log_lines[-30:]), produced, f"完成：{len(produced)} 張"


# ============================================================
# v3ctor.net scraper（Hugo 靜態站，<article> + <h1> + <p> 結構）
# ============================================================

V3CTOR_HOST = "v3ctor.net"


def scrape_v3ctor(url: str) -> dict:
    """從 v3ctor.net 抓單篇文章，回傳 {title, description, body, url}。

    僅支援 v3ctor.net 網域；用 stdlib（urllib + re）解析 Hugo 生成的 HTML。
    """
    from urllib.request import Request, urlopen
    from urllib.parse import urlparse

    if not url or not url.startswith("http"):
        raise ValueError("URL 必須以 http(s):// 開頭")
    if V3CTOR_HOST not in urlparse(url).netloc:
        raise ValueError(f"僅支援 {V3CTOR_HOST} 的 URL")

    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 ltx-mlx-director/scraper",
    })
    with urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")

    # 標題
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    title = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""

    # OG description（一句話 hook）
    m = re.search(
        r'<meta\s+(?:name|property)=["\'](?:og:)?description["\']\s+content=["\']([^"\']+)["\']',
        html,
    )
    description = m.group(1).strip() if m else ""

    # <article> 內的 <p> / <h2> / <h3>，按出現順序
    paragraphs: list[str] = []
    m = re.search(r"<article[^>]*>(.*?)</article>", html, re.S)
    if m:
        article = m.group(1)
        for tag_match in re.finditer(
            r"<(p|h2|h3)[^>]*>(.*?)</\1>", article, re.S,
        ):
            content = tag_match.group(2)
            text = re.sub(r"<[^>]+>", "", content)
            text = re.sub(r"\s+", " ", text).strip()
            # 過濾掉太短的雜訊（如「· 0 分鐘閱讀」「· 15 字」）
            if len(text) > 12 and "分鐘閱讀" not in text and "字 ·" not in text:
                paragraphs.append(text)

    return {
        "title": title,
        "description": description,
        "body": "\n\n".join(paragraphs),
        "url": url,
    }


_CLOSING_QUOTES = "」』）】"


def _merge_orphan_closers(shots: list[str]) -> list[str]:
    """中文引號 / 括號的關閉符若被分到下一鏡開頭，合回前一鏡。"""
    out: list[str] = []
    for s in shots:
        if out and s and s[0] in _CLOSING_QUOTES:
            out[-1] = out[-1] + s
        else:
            out.append(s)
    return out


def split_to_shots(text: str, mode: str, target_chars: int = 35) -> list[str]:
    """把長文拆成多鏡，每鏡一行（給 Tab 0 story_text 用）。

    mode:
      段落 - 每段一鏡（粗）
      句子 - 每句一鏡（細）
      智能 - 合併短句到 target_chars 字上限（平衡）

    所有模式都會做關閉符（」』）】）的孤兒修正。
    """
    if not text:
        return []
    if mode == "段落":
        return _merge_orphan_closers(
            [p.strip() for p in text.split("\n\n") if p.strip()]
        )
    if mode == "句子":
        parts = re.split(r"(?<=[。！？.!?])\s*", text)
        return _merge_orphan_closers([s.strip() for s in parts if s.strip()])
    # 智能
    parts = re.split(r"(?<=[。！？.!?])\s*", text)
    parts = _merge_orphan_closers([s.strip() for s in parts if s.strip()])
    shots: list[str] = []
    cur = ""
    for s in parts:
        if not s:
            continue
        if cur and (len(cur) + len(s)) > target_chars:
            shots.append(cur)
            cur = s
        else:
            cur = (cur + s).strip()
    if cur:
        shots.append(cur)
    return shots


SPLIT_MODE_LABELS = {
    "智能（合併短句到目標字數，最自然）": "智能",
    "句子（每個句號斷一鏡，最細）": "句子",
    "段落（每段一鏡，最粗）": "段落",
}


def list_v3ctor_articles(limit: int = 50) -> list[dict]:
    """從 v3ctor.net/sitemap.xml 取最新文章清單。

    回傳 [{title, url, date, slug}]，依 lastmod 倒序。
    """
    from urllib.request import Request, urlopen
    from urllib.parse import unquote

    req = Request(
        "https://v3ctor.net/sitemap.xml",
        headers={"User-Agent": "Mozilla/5.0 ltx-mlx-director/scraper"},
    )
    try:
        with urlopen(req, timeout=15) as r:
            xml = r.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    # sitemap entry 結構：<url><loc>...</loc><lastmod>...</lastmod><changefreq>...
    # 不能要求 </url> 結尾（中間還有 changefreq/priority）；只配對到 lastmod 即可
    entries = re.findall(
        r"<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>",
        xml,
    )
    result: list[dict] = []
    for url, date in entries:
        if "/stories/" not in url:
            continue
        slug = unquote(url.rstrip("/").rsplit("/", 1)[-1])
        if not re.match(r"^s\d+-", slug):
            continue  # 跳過分類索引頁
        # 抽中文標題（slug 中第一段中文）
        m = re.search(r"[一-鿿].*$", slug)
        title = m.group(0) if m else slug
        result.append({
            "title": title, "url": url,
            "date": date[:10], "slug": slug,
        })
    result.sort(key=lambda x: x["date"], reverse=True)
    return result[:limit]


def _format_article_label(a: dict) -> str:
    """dropdown 用：'2026-05-12 · 標題'"""
    return f"{a['date']} · {a['title'][:50]}"


# ============================================================
# 故事一鍵生成 pipeline 用的 subprocess 串流 helper
# ============================================================

STREAM_LOG_KEYWORDS = (
    "Denoising", "Loading", "Decoding", "Saved", "Time:", "Fetching",
    "Generating", "Error", "Traceback", "step", "Step",
)


def _stream_subprocess(cmd: list[str], log_lines: list[str], cwd: str | None = None):
    """以 generator 形式跑 subprocess，每出現關鍵 log 行就 yield 當前 log；
    最後 yield 一個 int (returncode)。
    """
    p = subprocess.Popen(
        cmd, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    assert p.stdout is not None
    for raw in p.stdout:
        for line in raw.replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            if any(k in line for k in STREAM_LOG_KEYWORDS):
                log_lines.append(f"     {line[-180:]}")
                yield "\n".join(log_lines[-40:])
    p.wait()
    yield p.returncode


# ============================================================
# 端到端：故事 → FLUX 靜圖 → LTX i2v → TTS+字幕 → concat
# ============================================================

STYLE_PRESETS = {
    "寫實電影風": ("cinematic, photorealistic, dramatic lighting, "
                "professional cinematography, shallow depth of field"),
    "奇幻動畫風": ("anime style, vibrant colors, soft cel shading, "
                "fantasy aesthetic, studio ghibli inspired"),
    "3D 渲染風": ("3D rendered, octane render, hyperrealistic, "
                "depth of field, volumetric lighting"),
    "水墨風": ("traditional Chinese ink painting, brush strokes, "
             "monochrome with subtle color accents, paper texture"),
    "懸疑昏暗風": ("dark moody atmosphere, dim lighting, mystery, "
                "fog, low key cinematography, film noir"),
    "新聞紀錄風": ("documentary style, natural lighting, gritty realism, "
                "candid composition, journalistic"),
    "自訂": "",
}


def stream_story_pipeline(
    story_text: str, style_preset: str, custom_style: str,
    sec_per_shot: float, motion_prompt: str,
    aspect: str, fps, seed, model: str, enhance: bool,
    voice: str, burn_subtitle: bool, mode_label: str,
    bgm_file_in=None, bgm_volume_in: float = -15.0,
    i2v_mode_label: str = list(I2V_MODES)[0],
    skip_image: bool = False,
    progress=gr.Progress(),
):
    """每行 = 一鏡頭。對每行：FLUX 出圖 → LTX i2v 雙錨 → TTS+字幕 → concat。"""
    if not story_text or not story_text.strip():
        yield "請先寫故事腳本（每行一鏡頭）", [], None, "未開始"
        return

    lines = [l.strip() for l in story_text.split("\n") if l.strip()]
    if not lines:
        yield "故事為空", [], None, "未開始"
        return

    if style_preset == "自訂":
        style_prompt = custom_style.strip() or "cinematic"
    else:
        style_prompt = STYLE_PRESETS.get(style_preset, "cinematic")
        if custom_style.strip():
            style_prompt = f"{style_prompt}, {custom_style.strip()}"

    motion = motion_prompt.strip() or "subtle cinematic camera movement"
    base_seed = (int(datetime.now().timestamp()) % (10 ** 8)
                  if int(seed) == -1 else int(seed))

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"story-{timestamp}"
    completed: list[str] = []
    log_lines: list[str] = [
        f"故事 {len(lines)} 鏡頭 | 風格: {style_preset} | "
        f"每鏡 {sec_per_shot}s | aspect {aspect} | voice {voice}",
        "",
    ]
    total = len(lines)

    for i, narration in enumerate(lines):
        shot_idx = i + 1
        progress((i) / total, desc=f"Shot {shot_idx}/{total}")
        log_lines.append(f"=== Shot {shot_idx}/{total} ===")
        log_lines.append(f"旁白: {narration[:80]}{'...' if len(narration) > 80 else ''}")
        yield "\n".join(log_lines[-40:]), completed, None, f"處理 Shot {shot_idx}/{total}"

        # --- 0/3: TTS 預跑取得旁白長度，必要時延長鏡頭秒數 ---
        actual_sec, pre_aiff = adjust_duration_to_narration(
            sec_per_shot, narration, voice, OUT_DIR, f"{base}_{shot_idx:02d}",
        )
        if actual_sec != sec_per_shot:
            log_lines.append(
                f"[0/3] 旁白較長，鏡頭秒數 {sec_per_shot}s → {actual_sec}s"
            )
            yield "\n".join(log_lines[-40:]), completed, None, \
                  f"Shot {shot_idx}: 延長為 {actual_sec}s"

        # --- 1/3: FLUX 靜圖（或跳過走純 t2v）---
        if skip_image:
            log_lines.append("[1/3] 跳過靜圖生成（純 t2v 模式）")
            yield "\n".join(log_lines[-40:]), completed, None, f"Shot {shot_idx}: 跳過靜圖"
            img_path_for_ltx = ""  # build_cmd 收到空字串 → t2v 路徑
        else:
            img_path = OUT_DIR / f"{base}_{shot_idx:02d}.png"
            img_prompt = f"{style_prompt}. {narration}"
            log_lines.append(f"[1/3] FLUX: {img_prompt[:120]}")
            yield "\n".join(log_lines[-40:]), completed, None, f"Shot {shot_idx}: 生圖中"

            flux_cmd = build_flux_cmd(
                img_prompt, list(FLUX_MODELS)[0], aspect,
                base_seed + i * 17, img_path,
            )
            rc_flux = None
            for item in _stream_subprocess(flux_cmd, log_lines):
                if isinstance(item, int):
                    rc_flux = item
                else:
                    yield item, completed, None, f"Shot {shot_idx}: 生圖中"

            if not (img_path.exists() and img_path.stat().st_size > 0):
                hint = _flux_error_hint("\n".join(log_lines[-15:]))
                log_lines.append(f"[1/3] FLUX 失敗 rc={rc_flux}，跳過此鏡{hint}")
                yield "\n".join(log_lines[-40:]), completed, None, f"Shot {shot_idx}: FLUX 失敗"
                continue
            log_lines.append(f"[1/3] FLUX 完成: {img_path.name}")
            img_path_for_ltx = str(img_path)

        # --- 2/3: LTX（t2v 或 i2v）---
        vid_path = OUT_DIR / f"{base}_{shot_idx:02d}.mp4"
        # 純 t2v 模式下，prompt 用 motion + narration 才能描述場景
        ltx_prompt = (
            f"{motion}. {style_prompt}. {narration}"
            if skip_image else motion
        )
        log_lines.append(
            f"[2/3] LTX {'t2v' if skip_image else 'i2v'}: {ltx_prompt[:120]}"
        )
        yield "\n".join(log_lines[-40:]), completed, None, f"Shot {shot_idx}: 動畫中"

        ltx_cmd, mode_tag = build_cmd(
            ltx_prompt, actual_sec, img_path_for_ltx,
            aspect, int(fps), mode_label,
            base_seed + i * 17, model, enhance, vid_path,
            i2v_mode=I2V_MODES.get(i2v_mode_label, "ic-lora-canny"),
        )
        if mode_tag == "ic-lora-canny":
            log_lines.append(f"[2/3] LTX ic-lora canny 結構鎖")
        elif mode_tag == "two-stage-dual-anchor":
            log_lines.append(f"[2/3] LTX two-stage 雙錨")
        rc_ltx = None
        for item in _stream_subprocess(ltx_cmd, log_lines, cwd=str(REPO_DIR)):
            if isinstance(item, int):
                rc_ltx = item
            else:
                yield item, completed, None, f"Shot {shot_idx}: 動畫中"

        if not (vid_path.exists() and vid_path.stat().st_size > 0):
            log_lines.append(f"[2/3] LTX 失敗 rc={rc_ltx}，跳過此鏡")
            yield "\n".join(log_lines[-40:]), completed, None, f"Shot {shot_idx}: 失敗"
            continue
        log_lines.append(f"[2/3] LTX 完成: {vid_path.name}")

        # --- 3/3: TTS + 字幕 ---
        log_lines.append(f"[3/3] TTS + 字幕（{voice}）")
        yield "\n".join(log_lines[-40:]), completed, None, f"Shot {shot_idx}: 旁白中"

        final_shot, msg = post_process_shot(
            vid_path, narration, actual_sec, voice, bool(burn_subtitle),
            pre_audio=pre_aiff,
        )
        log_lines.append(f"[3/3] {msg}")
        completed.append(str(final_shot))
        yield "\n".join(log_lines[-40:]), completed, None, f"Shot {shot_idx}: 完成"

    # --- 最終 concat ---
    if not completed:
        log_lines.append("\n沒有成功的鏡頭，無法串接")
        yield "\n".join(log_lines[-40:]), completed, None, "全部失敗"
        return

    log_lines.append(f"\n=== 串接 {len(completed)} 鏡頭 ===")
    yield "\n".join(log_lines[-40:]), completed, None, "串接中"

    final_path, concat_msg = concat_shots(completed, bgm_file_in, bgm_volume_in)
    log_lines.append(f"=== {concat_msg}")
    progress(1.0, desc="完成")
    yield "\n".join(log_lines[-40:]), completed, final_path, f"完成: {concat_msg}"


# ============================================================
# ic-lora + canny 結構鎖定（最強身份保留）
# ============================================================

IC_LORA_UNION_CONTROL = "Lightricks/LTX-2.3-22b-IC-LoRA-Union-Control"
HF_CACHE_DIR = Path.home() / ".cache" / "huggingface" / "hub"


def is_lora_cached(repo_id: str) -> tuple[bool, float]:
    """檢查 HF hub cache 中是否已有此 repo。回傳 (是否存在, 大小 GB)。"""
    folder_name = "models--" + repo_id.replace("/", "--")
    target = HF_CACHE_DIR / folder_name
    if not target.exists():
        return False, 0.0
    total = 0
    for p in target.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total > 0, total / 1024 / 1024 / 1024


def generate_canny_control(image_path: Path, width: int, height: int,
                            frames: int, fps: int, out_path: Path) -> bool:
    """從靜圖產生 canny 邊緣的控制影片（同一張圖複製 N 幀）。

    上游 ltx-2-mlx static-scene I2V recipe 的標準做法：
    - scale + crop 到目標尺寸
    - edgedetect=mode=canny 取邊
    - yuv420p 格式輸出
    """
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-i", str(image_path),
        "-vf", (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            "edgedetect=mode=canny:low=0.1:high=0.4,"
            "format=yuv420p"
        ),
        "-frames:v", str(frames),
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        str(out_path),
    ]
    subprocess.run(cmd, capture_output=True, text=True)
    return out_path.exists() and out_path.stat().st_size > 0


def build_ic_lora_cmd(prompt: str, image_path: Path, control_video: Path,
                      width: int, height: int, frames: int, fps: int,
                      seed: int, model: str, out_path: Path,
                      lora_repo: str = IC_LORA_UNION_CONTROL) -> list[str]:
    """構建 ltx-2-mlx ic-lora 命令，使用 Union Control + canny 控制每一幀。

    這是上游推薦的 static-scene identity preservation pattern，整段影片
    身份完全保留（vs --two-stage 雙錨只保首尾）。
    比 --two-stage 快 4×（distilled defaults: 8+3 steps, no CFG）。
    """
    return [
        str(LTX_BIN), "ic-lora",
        "-p", prompt,
        "--lora", lora_repo, "1.0",
        "--video-conditioning", str(control_video), "1.0",
        "--image", str(image_path),
        "--low-ram",
        "--width", str(width), "--height", str(height),
        "--frames", str(frames),
        "--frame-rate", str(fps),
        "--seed", str(int(seed)),
        "--model", model,
        "-o", str(out_path),
    ]


def build_cmd(prompt: str, seconds: float, image: str, aspect: str, fps: int,
              mode_label: str, seed: int, model: str, enhance: bool,
              out_path: Path, i2v_mode: str = "ic-lora-canny",
              ) -> tuple[list[str], str]:
    """構建 ltx-2-mlx 命令，依 i2v_mode 切換。

    回傳 (cmd, mode_tag)：
      mode_tag in {"ic-lora-canny", "two-stage-dual-anchor", "t2v"}

    模式：
      - ic-lora-canny：有圖時，從圖生 canny 控制影片，跑 ic-lora（最強身份保留，
        上游 static-scene I2V recipe，比 two-stage 快 4×）
      - two-stage-dual-anchor：有圖時跑 --two-stage + 頭尾雙錨（中等保留）
      - 無圖：用 mode_label (distilled / two-stages-hq) 做純 t2v

    注意 ic-lora 模式會在 out_path 同目錄寫一個 *_canny.mp4 控制影片。
    """
    w, h = ASPECT_WH[aspect]
    frames = duration_to_frames(seconds, fps)

    has_image = bool(image and image.strip()
                     and Path(image).expanduser().exists())

    if has_image and i2v_mode == "ic-lora-canny":
        img_path = Path(image).expanduser()
        control_video = out_path.parent / f"{out_path.stem}_canny.mp4"
        if generate_canny_control(img_path, w, h, frames, int(fps), control_video):
            cmd = build_ic_lora_cmd(
                prompt, img_path, control_video,
                w, h, frames, int(fps), int(seed), model, out_path,
            )
            if enhance:
                cmd += ["--enhance-prompt"]
            return cmd, "ic-lora-canny"
        # canny 失敗則 fallthrough 到 two-stage

    if has_image:
        # 雙錨 two-stage：--one-stage 不支援多錨，--two-stage 雖然 help 寫 requires q8 但 q4 實測可用
        pipe = "--two-stage"
        cmd = [
            str(LTX_BIN), "generate",
            "-p", prompt,
            pipe, "--low-ram",
            "--width", str(w), "--height", str(h),
            "--frames", str(frames),
            "--frame-rate", str(fps),
            "--seed", str(int(seed)),
            "--model", model,
            "-o", str(out_path),
        ]
        img_path = str(Path(image).expanduser())
        cmd += ["--image", img_path, "0", "1.0"]
        cmd += ["--image", img_path, str(frames - 1), "1.0"]
        if enhance:
            cmd += ["--enhance-prompt"]
        return cmd, "two-stage-dual-anchor"

    # 純 t2v
    pipe = MODE_FLAGS[mode_label]
    cmd = [
        str(LTX_BIN), "generate",
        "-p", prompt,
        pipe, "--low-ram",
        "--width", str(w), "--height", str(h),
        "--frames", str(frames),
        "--frame-rate", str(fps),
        "--seed", str(int(seed)),
        "--model", model,
        "-o", str(out_path),
    ]
    if enhance:
        cmd += ["--enhance-prompt"]
    return cmd, "t2v"


def stream_generate(df: pd.DataFrame, aspect, fps, mode_label, seed, model,
                    enhance, voice, burn_subtitle, i2v_mode_label,
                    progress=gr.Progress()):
    """逐鏡呼叫 ltx-2-mlx，每鏡完成後做 TTS+字幕後處理（若有旁白文字）。"""
    if df is None or len(df) == 0:
        yield "請先新增至少一個鏡頭", [], None
        return

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"director-{timestamp}"
    shots_done: list[str] = []
    log_lines: list[str] = []
    total = len(df)

    for i, row in df.iterrows():
        try:
            seconds = float(row["秒"])
            prompt = str(row["Prompt"]).strip()
            image = str(row.get("i2v 圖片路徑(選填)", "") or "").strip()
            narration = str(row.get("旁白文字(選填)", "") or "").strip()
        except Exception as e:
            log_lines.append(f"[{i+1}] 無效列，跳過: {e}")
            yield "\n".join(log_lines[-30:]), shots_done, None
            continue
        if not prompt:
            log_lines.append(f"[{i+1}] 空 prompt，跳過")
            yield "\n".join(log_lines[-30:]), shots_done, None
            continue

        out_path = OUT_DIR / f"{base}_{int(i)+1:02d}.mp4"
        i2v_mode = I2V_MODES.get(i2v_mode_label, "ic-lora-canny")
        cmd, mode_tag = build_cmd(prompt, seconds, image, aspect, int(fps),
                                   mode_label, int(seed), model, enhance, out_path,
                                   i2v_mode=i2v_mode)

        progress((i) / total, desc=f"Shot {i+1}/{total}: {prompt[:30]}")
        mode_desc = {"ic-lora-canny": " | ic-lora canny 結構鎖",
                     "two-stage-dual-anchor": " | two-stage 雙錨",
                     "t2v": ""}.get(mode_tag, "")
        lock_tag = mode_desc
        i2v_lock = mode_tag != "t2v"
        narr_tag = " | 旁白" if narration else ""
        log_lines.append(f"\n=== Shot {i+1}/{total} | {seconds}s | {aspect}{lock_tag}{narr_tag} ===")
        log_lines.append(f"Prompt: {prompt}")
        if i2v_lock:
            log_lines.append(f"Image: {image}")
        if narration:
            log_lines.append(f"旁白: {narration[:60]}{'...' if len(narration) > 60 else ''}")
        yield "\n".join(log_lines[-30:]), shots_done, None

        p = subprocess.Popen(
            cmd, cwd=str(REPO_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        assert p.stdout is not None
        for raw in p.stdout:
            for line in raw.replace("\r", "\n").split("\n"):
                line = line.strip()
                if not line:
                    continue
                if any(k in line for k in ("Denoising", "Loading", "Decoding",
                                            "Saved", "Time:", "Fetching",
                                            "Error", "Traceback")):
                    log_lines.append(f"   {line[-180:]}")
                    yield "\n".join(log_lines[-30:]), shots_done, None
        p.wait()
        if p.returncode == 0 and out_path.exists():
            final_shot = out_path
            if narration or burn_subtitle:
                final_shot, msg = post_process_shot(
                    out_path, narration, seconds, voice, bool(burn_subtitle),
                )
                log_lines.append(f"   -> {msg}")
            shots_done.append(str(final_shot))
            log_lines.append(f"   -> 完成: {final_shot.name}")
        else:
            log_lines.append(f"   -> 失敗 exit={p.returncode}")
        yield "\n".join(log_lines[-30:]), shots_done, None

    progress(1.0, desc="所有鏡頭完成")
    log_lines.append(f"\n全部完成。{len(shots_done)} / {total} 鏡頭成功。")
    yield "\n".join(log_lines[-30:]), shots_done, None


def _mix_background_music(video_in: Path, music_in: Path, volume_db: float,
                           video_out: Path) -> bool:
    """把 music_in 以 volume_db (dB) 混入 video_in 已有的音軌，輸出 video_out。"""
    import math
    vol_lin = 10 ** (volume_db / 20.0)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_in),
        "-stream_loop", "-1", "-i", str(music_in),
        "-filter_complex",
        f"[1:a]volume={vol_lin:.3f}[bg];"
        f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(video_out),
    ]
    subprocess.run(cmd, capture_output=True, text=True)
    return video_out.exists() and video_out.stat().st_size > 0


def concat_shots(shot_paths: list[str], music_file=None, music_volume_db: float = -15.0):
    """串接已生成的鏡頭，可選擇混入背景音樂。

    music_file: gradio File 物件或路徑字串；None 表示不混音樂
    music_volume_db: 背景音樂相對於原音的 dB（負值較安靜，預設 -15dB）

    Bug 修復：ffmpeg concat + -c copy 警告會回非零但仍產出有效檔案，改為依
    「檔案存在且 > 0 byte」判定。
    """
    if not shot_paths:
        return None, "尚無已生成的鏡頭"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    list_file = OUT_DIR / f"final-{timestamp}.concat.txt"
    final_path = OUT_DIR / f"final-{timestamp}.mp4"
    list_file.write_text("\n".join(f"file '{p}'" for p in shot_paths))

    proc = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", str(final_path)],
        capture_output=True, text=True,
    )
    concat_ok = final_path.exists() and final_path.stat().st_size > 0

    if not concat_ok:
        # stream copy 失敗 → 重編碼
        proc2 = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(final_path)],
            capture_output=True, text=True,
        )
        concat_ok = final_path.exists() and final_path.stat().st_size > 0
        if not concat_ok:
            err = (proc2.stderr or proc.stderr or "")[-400:]
            return None, f"ffmpeg 失敗: {err}"

    # 解析音樂檔案路徑
    music_path = None
    if music_file is not None:
        p = music_file.name if hasattr(music_file, "name") else str(music_file)
        if p and Path(p).exists():
            music_path = Path(p)

    if music_path is None:
        rc_note = "" if proc.returncode == 0 else f"（rc={proc.returncode}）"
        return str(final_path), f"串接完成: {final_path.name}{rc_note}"

    # 混入背景音樂
    final_with_music = OUT_DIR / f"final-{timestamp}_bgm.mp4"
    if _mix_background_music(final_path, music_path,
                              float(music_volume_db), final_with_music):
        return str(final_with_music), \
            f"串接 + 配樂完成: {final_with_music.name}（音樂 {music_volume_db}dB）"
    return str(final_path), \
        f"串接完成但配樂混入失敗，回傳純串接版: {final_path.name}"


def build_keyframe_cmd(prompt: str, start_img: str, end_img: str, seconds: float,
                       aspect: str, fps: int, seed: int, model: str,
                       out_path: Path) -> list[str]:
    """構建 ltx-2-mlx keyframe 命令（兩張圖之間生成轉場）。

    keyframe 子命令是真正的「圖到圖」過渡：起始幀 = start 圖、結束幀 = end 圖，
    中間幾秒由模型補出連貫運動。這比 generate --image 的單錨 i2v 對身份保留更強。
    """
    w, h = ASPECT_WH[aspect]
    frames = duration_to_frames(seconds, fps)
    return [
        str(LTX_BIN), "keyframe",
        "-p", prompt or "smooth cinematic transition",
        "--low-ram",
        "--start", str(Path(start_img).expanduser()),
        "--end", str(Path(end_img).expanduser()),
        "--width", str(w), "--height", str(h),
        "--frames", str(frames),
        "--frame-rate", str(fps),
        "--seed", str(int(seed)),
        "--model", model,
        "-o", str(out_path),
    ]


def stream_keyframes(files, prompt, sec_per_seg, aspect, fps, seed, model,
                      progress=gr.Progress()):
    """逐段呼叫 ltx-2-mlx keyframe，N 張圖產 N-1 段轉場，串流回 UI。"""
    if not files or len(files) < 2:
        yield "至少需要 2 張圖片", [], None
        return

    paths: list[str] = []
    for f in files:
        p = f.name if hasattr(f, "name") else str(f)
        if Path(p).exists():
            paths.append(p)

    if len(paths) < 2:
        yield "有效圖片不足 2 張（請確認上傳成功）", [], None
        return

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"keyframe-{timestamp}"
    shots_done: list[str] = []
    log_lines: list[str] = [
        f"關鍵幀串接：{len(paths)} 張圖 → {len(paths) - 1} 段轉場",
    ]
    yield "\n".join(log_lines), shots_done, None

    n_segments = len(paths) - 1
    for i in range(n_segments):
        start = paths[i]
        end = paths[i + 1]
        out_path = OUT_DIR / f"{base}_{i+1:02d}.mp4"
        cmd = build_keyframe_cmd(prompt, start, end, sec_per_seg, aspect,
                                  int(fps), int(seed), model, out_path)

        progress(i / n_segments, desc=f"Seg {i+1}/{n_segments}")
        log_lines.append(f"\n=== Segment {i+1}/{n_segments} | {sec_per_seg}s ===")
        log_lines.append(f"  start: {Path(start).name}")
        log_lines.append(f"  end:   {Path(end).name}")
        yield "\n".join(log_lines[-30:]), shots_done, None

        p = subprocess.Popen(
            cmd, cwd=str(REPO_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        assert p.stdout is not None
        for raw in p.stdout:
            for line in raw.replace("\r", "\n").split("\n"):
                line = line.strip()
                if not line:
                    continue
                if any(k in line for k in ("Denoising", "Loading", "Decoding",
                                            "Saved", "Time:", "Fetching",
                                            "Error", "Traceback")):
                    log_lines.append(f"   {line[-180:]}")
                    yield "\n".join(log_lines[-30:]), shots_done, None
        p.wait()
        if p.returncode == 0 and out_path.exists():
            shots_done.append(str(out_path))
            log_lines.append(f"   -> 完成: {out_path.name}")
        else:
            log_lines.append(f"   -> 失敗 exit={p.returncode}")
        yield "\n".join(log_lines[-30:]), shots_done, None

    progress(1.0, desc="全部轉場完成")
    log_lines.append(f"\n完成。{len(shots_done)} / {n_segments} 段成功。")
    yield "\n".join(log_lines[-30:]), shots_done, None


def export_json(df: pd.DataFrame, aspect, fps, mode_label, seed, model, enhance,
                 voice, burn_subtitle):
    if df is None:
        df = make_initial_df()
    shots = [
        {
            "duration": float(r["秒"]),
            "prompt": str(r["Prompt"]),
            "image": str(r.get("i2v 圖片路徑(選填)", "") or ""),
            "narration": str(r.get("旁白文字(選填)", "") or ""),
        }
        for _, r in df.iterrows()
    ]
    data = {
        "settings": {
            "aspect": aspect, "fps": int(fps), "mode": mode_label,
            "seed": int(seed), "model": model, "enhance": bool(enhance),
            "voice": voice, "burn_subtitle": bool(burn_subtitle),
        },
        "shots": shots,
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def import_json(text: str):
    if not text or not text.strip():
        return (gr.update(),) * 9
    data = json.loads(text)
    s = data.get("settings", {})
    shots = data.get("shots", [])
    df = pd.DataFrame(
        [[sh.get("duration", 4), sh.get("prompt", ""),
          sh.get("image", ""), sh.get("narration", "")]
         for sh in shots],
        columns=["秒", "Prompt", "i2v 圖片路徑(選填)", "旁白文字(選填)"],
    )
    return (
        df,
        s.get("aspect", "16:9"),
        s.get("fps", 24),
        s.get("mode", list(MODE_FLAGS)[0]),
        s.get("seed", -1),
        s.get("model", "dgrauet/ltx-2.3-mlx-q4"),
        s.get("enhance", False),
        s.get("voice", "Meijia"),
        s.get("burn_subtitle", True),
    )


def move_row(df: pd.DataFrame, selected: int | None, delta: int) -> pd.DataFrame:
    if df is None or selected is None:
        return df
    idx = int(selected)
    j = idx + delta
    if 0 <= idx < len(df) and 0 <= j < len(df):
        df = df.copy()
        df.iloc[[idx, j]] = df.iloc[[j, idx]].values
    return df


def add_row(df: pd.DataFrame, default_dur: float) -> pd.DataFrame:
    if df is None:
        df = make_initial_df().iloc[0:0]
    new = pd.DataFrame([[default_dur, "", "", ""]], columns=df.columns)
    return pd.concat([df, new], ignore_index=True)


def push_flux_to_storyboard(flux_paths: list[str], df: pd.DataFrame,
                             default_dur: float) -> pd.DataFrame:
    """把 Tab 3 生成的圖一鍵新增為 Tab 1 分鏡列。"""
    if not flux_paths:
        return df if df is not None else make_initial_df()
    if df is None:
        df = make_initial_df().iloc[0:0]
    rows = [
        [default_dur, "subtle cinematic zoom in, smooth camera", p, ""]
        for p in flux_paths if Path(p).exists()
    ]
    if not rows:
        return df
    new_df = pd.DataFrame(rows, columns=df.columns)
    return pd.concat([df, new_df], ignore_index=True)


def delete_row(df: pd.DataFrame, selected: int | None) -> pd.DataFrame:
    if df is None or selected is None:
        return df
    idx = int(selected)
    if 0 <= idx < len(df):
        df = df.drop(df.index[idx]).reset_index(drop=True)
    return df


CSS = """
#log_box textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.shot-strip video { max-height: 200px; }
"""

with gr.Blocks(title="LTX-2.3 Director") as app:
    gr.Markdown("# LTX-2.3 Director\n本地（MLX）影片導演面板。先設全域、再寫分鏡、按生成。")

    with gr.Row():
        aspect = gr.Dropdown(list(ASPECT_WH), value="16:9", label="畫面比例")
        fps = gr.Dropdown([24, 30, 60], value=24, label="FPS")
        mode = gr.Dropdown(list(MODE_FLAGS), value=list(MODE_FLAGS)[0], label="模式")
        seed = gr.Number(value=-1, label="Seed（-1 = 隨機）", precision=0)
        default_dur = gr.Number(value=4, label="新鏡頭預設秒數", precision=1)
    with gr.Row():
        model = gr.Textbox(value="dgrauet/ltx-2.3-mlx-q4", label="模型 (HF repo)")
        enhance = gr.Checkbox(value=False, label="Gemma 自動改寫 prompt")
    with gr.Row():
        voice = gr.Dropdown(
            TTS_VOICES_ZH_TW, value="Meijia",
            label="TTS 語音（zh_TW，旁白用）",
        )
        burn_subtitle = gr.Checkbox(
            value=True, label="燒入字幕（中文 PingFang）",
        )
    with gr.Row():
        with gr.Column(scale=3):
            i2v_mode_select = gr.Radio(
                choices=list(I2V_MODES),
                value=list(I2V_MODES)[0],
                label="i2v 識別保留模式（有參考圖時生效）",
                info="ic-lora 用 canny 邊緣控制每一幀，全程身份鎖定，比 two-stage 快",
            )
        with gr.Column(scale=1):
            _lora_ok, _lora_gb = is_lora_cached(IC_LORA_UNION_CONTROL)
            _lora_status_text = (
                f"IC-LoRA Union Control: 已下載 ({_lora_gb:.1f} GB)"
                if _lora_ok
                else "IC-LoRA Union Control: 首跑會下載"
            )
            lora_status = gr.Markdown(_lora_status_text)
            lora_recheck_btn = gr.Button("重新檢查 LoRA 狀態", size="sm")

    def _refresh_lora_status():
        ok, gb = is_lora_cached(IC_LORA_UNION_CONTROL)
        return (f"IC-LoRA Union Control: 已下載 ({gb:.1f} GB)"
                if ok else "IC-LoRA Union Control: 首跑會下載")
    lora_recheck_btn.click(_refresh_lora_status, None, lora_status)
    with gr.Accordion("背景音樂（選填，會在最終 concat 後混入）", open=False):
        with gr.Row():
            bgm_file = gr.File(
                label="背景音樂檔（mp3/wav/m4a）",
                file_types=["audio"], file_count="single",
                height=80, scale=2,
            )
            bgm_volume = gr.Slider(
                -40, 0, value=-15, step=1, scale=1,
                label="背景音樂音量（dB，越負越小聲；旁白約 0dB）",
            )

    completed_state = gr.State([])
    flux_state = gr.State([])
    # Gradio 6.x tab 切換有時會 visual reset textbox，用 State 備份 story_text 內容
    story_text_state = gr.State("")

    tabs = gr.Tabs()
    with tabs:
        # ============================ Tab 0: 故事一鍵生成（推薦）==============================
        with gr.Tab("故事一鍵生成（推薦）"):
            gr.Markdown(
                "**預設工作流**：從 v3ctor.net 選一篇 → 按「🚀 抓 + 一鍵生成」"
                "→ 自動拆鏡 / 動畫 / 旁白 / 字幕 / 串接，產出最終影片。\n\n"
                "想自寫劇本？跳過下方 v3ctor 區，直接在「故事腳本」textbox 編輯，"
                "按「一鍵生成全部」即可（每行 = 一鏡）。"
            )

            # --- v3ctor.net 文章選取（預設工作流入口）---
            gr.Markdown("### 從 v3ctor.net 選文章")
            _initial_articles = list_v3ctor_articles(50)
            _initial_choices = [
                (_format_article_label(a), a["url"]) for a in _initial_articles
            ]
            _initial_url = _initial_articles[0]["url"] if _initial_articles else ""

            with gr.Row():
                v3_article_dd = gr.Dropdown(
                    choices=_initial_choices,
                    value=_initial_url or None,
                    label=f"最新文章（共 {len(_initial_articles)} 篇，依 lastmod 排序）",
                    scale=4, interactive=True,
                )
                v3_refresh_btn = gr.Button("重新載入清單", scale=1)
            with gr.Row():
                v3_url = gr.Textbox(
                    value=_initial_url,
                    label="URL（可手動改成任何 v3ctor.net/stories/... 的網址）",
                    scale=4,
                )
            with gr.Row():
                v3_split_mode = gr.Dropdown(
                    list(SPLIT_MODE_LABELS), value=list(SPLIT_MODE_LABELS)[0],
                    label="拆鏡策略", scale=2,
                )
                v3_target_chars = gr.Slider(
                    15, 80, value=35, step=5,
                    label="目標字數／鏡（智能模式才生效）", scale=2,
                )
            with gr.Row():
                v3_fetch_btn = gr.Button(
                    "抓取（預覽用，只填入下方腳本）", scale=1,
                )
            v3_info = gr.Markdown("")

            # dropdown 變更 → 同步 URL textbox
            v3_article_dd.change(
                lambda url: url or "", v3_article_dd, v3_url,
            )

            # 重新載入清單
            def _reload_articles():
                arts = list_v3ctor_articles(50)
                choices = [(_format_article_label(a), a["url"]) for a in arts]
                first = arts[0]["url"] if arts else None
                return (
                    gr.update(choices=choices, value=first),
                    first or "",
                    f"已重新載入：{len(arts)} 篇",
                )
            v3_refresh_btn.click(
                _reload_articles, None, [v3_article_dd, v3_url, v3_info],
            )

            gr.Markdown("### 故事腳本（每行 = 一鏡，可自寫或讓上方抓取自動填）")

            story_text = gr.Textbox(
                label="故事腳本（每行一鏡，可空白讓上方 v3ctor 自動填）",
                value="",
                lines=8,
                placeholder=(
                    "空白時：上方按 🚀 會先抓 v3ctor 文章再生成。\n"
                    "想自寫：直接打字，每行一鏡（範例：「在某個小漁村...」）。"
                ),
            )

            with gr.Row():
                story_style = gr.Dropdown(
                    list(STYLE_PRESETS), value="寫實電影風",
                    label="視覺風格（套到所有鏡頭圖 prompt）",
                )
                story_custom_style = gr.Textbox(
                    label="風格 prompt 補強（會接在風格之後，英文較好）",
                    placeholder="golden hour, fog, low angle",
                    lines=2,
                )

            with gr.Row():
                story_sec = gr.Number(
                    value=4, label="每鏡秒數", precision=1,
                    minimum=2, maximum=10,
                )
                story_motion = gr.Textbox(
                    label="動作 prompt（套到所有鏡頭的 LTX，描述鏡頭運動）",
                    value="subtle cinematic zoom in, smooth camera, atmospheric",
                    lines=2,
                )

            with gr.Row():
                skip_image_gen = gr.Checkbox(
                    value=True,
                    label="純 t2v 模式（推薦預設，免裝靜圖模型）",
                    info="預設勾選 → 直接 LTX 文字轉影片，最穩定可用。"
                          "取消勾選才會跑 FLUX/Z-Image 生圖（要 HF 登入或撞 mflux bug）。",
                )

            def _fetch_v3ctor_and_fill(url, mode_label, target):
                if not url or not url.strip():
                    return gr.update(), "請先貼 v3ctor.net 的文章 URL"
                try:
                    article = scrape_v3ctor(url.strip())
                except Exception as e:
                    return gr.update(), f"抓取失敗：`{type(e).__name__}` {e}"
                mode = SPLIT_MODE_LABELS.get(mode_label, "智能")
                shots = split_to_shots(article["body"], mode, int(target))
                if not shots:
                    return gr.update(), f"抓到《{article['title']}》但拆不出鏡頭"
                avg = sum(len(s) for s in shots) // len(shots)
                hook = f"\n\n_{article['description']}_" if article['description'] else ""
                info = (
                    f"**《{article['title']}》**{hook}\n\n"
                    f"已拆出 **{len(shots)} 鏡**（{mode}，平均 {avg} 字/鏡），"
                    f"填入下方故事腳本。可手動調整後再按生成。"
                )
                return "\n".join(shots), info

            v3_fetch_btn.click(
                _fetch_v3ctor_and_fill,
                [v3_url, v3_split_mode, v3_target_chars],
                [story_text, v3_info],
            )

            story_run_btn = gr.Button(
                "🚀 生成影片（腳本空白會自動從上方 URL 抓取）",
                variant="primary", size="lg",
            )
            story_status = gr.Markdown("尚未開始")
            story_log = gr.Textbox(
                label="進度", lines=14, max_lines=24,
                autoscroll=True, elem_id="log_box",
            )
            story_final_video = gr.Video(label="最終影片（自動串接）", height=420)

            gr.Markdown(
                "**內部流程（每鏡）**\n"
                "1. 預跑 TTS 取得旁白實際秒數（若超過設定值自動延長鏡頭）\n"
                "2. 靜圖生成（預設 z-image-turbo，開放權重）\n"
                "3. LTX ic-lora canny 結構鎖 i2v（首尾每幀全程鎖定身份）\n"
                "4. macOS `say` 出 TTS aiff + ffmpeg 燒中文字幕 + mux 音軌\n"
                "5. 全部完成後 ffmpeg concat 為最終 mp4（可選背景音樂混入）\n\n"
                "靜圖預設 z-image-turbo（無需登入）；想用 FLUX 請先到 Tab 3 看登入說明。\n"
                "想細調個別鏡頭：切到 **手動分鏡** Tab，分鏡內容已分享。"
            )

        # ============================== Tab 1: 分鏡腳本 ==============================
        with gr.Tab("手動分鏡（每鏡一圖 i2v）"):
            gr.Markdown(
                "點選列以選取，再用按鈕移動或刪除。可以直接編輯儲存格。\n"
                "**參考圖**：點選任一列 → 上傳圖 → 「套用到選取列」。"
                "有圖的鏡頭會自動切換到 `--two-stage` 並用頭尾雙錨點，避免圖被忽略。"
            )

            storyboard = gr.Dataframe(
                value=make_initial_df(),
                headers=["秒", "Prompt", "i2v 圖片路徑(選填)", "旁白文字(選填)"],
                datatype=["number", "str", "str", "str"],
                row_count=(1, "dynamic"),
                column_count=(4, "fixed"),
                interactive=True,
                wrap=True,
                label="storyboard",
            )
            selected_row = gr.State(None)
            selected_label = gr.Markdown("未選取列")

            with gr.Row():
                add_btn = gr.Button("新增鏡頭")
                del_btn = gr.Button("刪除選取鏡頭", variant="stop")
                up_btn = gr.Button("選取列上移")
                dn_btn = gr.Button("選取列下移")

            gr.Markdown("### 參考圖（i2v 起手式）")
            with gr.Row():
                with gr.Column(scale=2):
                    selected_image = gr.Image(
                        label="參考圖", type="filepath", height=240,
                        sources=["upload", "clipboard"],
                    )
                with gr.Column(scale=1):
                    apply_image_btn = gr.Button("套用到選取列", variant="primary")
                    clear_image_btn = gr.Button("清除選取列圖片", variant="stop")
                    gr.Markdown(
                        "- 必須先點選分鏡表中的列\n"
                        "- 套用後該列的 i2v 路徑欄會顯示圖片絕對路徑"
                    )

            def on_select(evt: gr.SelectData, df):
                idx = int(evt.index[0]) if (evt and evt.index is not None) else None
                label = "未選取列" if idx is None else f"已選取：第 {idx + 1} 列"
                img = None
                if idx is not None and df is not None and 0 <= idx < len(df):
                    path = str(df.iat[idx, 2] or "").strip()
                    if path and Path(path).exists():
                        img = path
                return idx, label, img

            storyboard.select(
                on_select,
                inputs=[storyboard],
                outputs=[selected_row, selected_label, selected_image],
            )

            def apply_image_to_row(img_path, df, sel):
                if sel is None or df is None:
                    return df
                idx = int(sel)
                if not (0 <= idx < len(df)):
                    return df
                df = df.copy()
                df.iat[idx, 2] = img_path or ""
                return df

            def clear_image_in_row(df, sel):
                if sel is None or df is None:
                    return df, None
                idx = int(sel)
                if not (0 <= idx < len(df)):
                    return df, None
                df = df.copy()
                df.iat[idx, 2] = ""
                return df, None

            apply_image_btn.click(
                apply_image_to_row,
                [selected_image, storyboard, selected_row],
                storyboard,
            )
            clear_image_btn.click(
                clear_image_in_row,
                [storyboard, selected_row],
                [storyboard, selected_image],
            )

            def handle_add(df, default_d):
                return add_row(df, default_d), None, "未選取列"

            def handle_delete(df, sel):
                return delete_row(df, sel), None, "未選取列"

            def handle_move(df, sel, delta):
                df2 = move_row(df, sel, delta)
                if sel is None:
                    return df2, None, "未選取列"
                new_idx = int(sel) + delta
                if 0 <= new_idx < len(df2):
                    return df2, new_idx, f"已選取：第 {new_idx + 1} 列"
                return df2, sel, f"已選取：第 {int(sel) + 1} 列"

            add_btn.click(handle_add, [storyboard, default_dur],
                          [storyboard, selected_row, selected_label])
            del_btn.click(handle_delete, [storyboard, selected_row],
                          [storyboard, selected_row, selected_label])
            up_btn.click(lambda df, s: handle_move(df, s, -1),
                         [storyboard, selected_row],
                         [storyboard, selected_row, selected_label])
            dn_btn.click(lambda df, s: handle_move(df, s, +1),
                         [storyboard, selected_row],
                         [storyboard, selected_row, selected_label])

            gr.Markdown("### 生成")
            gen_btn = gr.Button("開始生成全部鏡頭", variant="primary", size="lg")
            log_box = gr.Textbox(label="進度", lines=12, max_lines=20,
                                  autoscroll=True, elem_id="log_box")

            gr.Markdown("### 匯出 / 匯入 storyboard")
            with gr.Row():
                export_btn = gr.Button("匯出為 JSON")
                import_btn = gr.Button("從下方 JSON 匯入")
            json_box = gr.Code(label="Storyboard JSON", language="json", lines=10)

        # ============================ Tab 2: 圖片轉場 ==============================
        with gr.Tab("圖片轉場（多圖 keyframe）"):
            gr.Markdown(
                "上傳 N 張圖（依想要的順序），系統會用 `ltx-2-mlx keyframe` "
                "在每兩張之間補出一段轉場影片。共產出 **N-1 段**，"
                "可在下方「最終串接」串成一支完整影片。\n\n"
                "與 i2v 起手式的差別：**keyframe 模式同時鎖定起始與結束幀**，"
                "整段轉場身份保留更強，適合「定格動畫」、「角色變化過程」、"
                "「場景轉場」等需求。"
            )

            keyframe_files = gr.File(
                label="上傳圖片（多選，依順序為轉場序列）",
                file_count="multiple",
                file_types=["image"],
                height=220,
            )
            keyframe_gallery = gr.Gallery(
                label="圖片預覽（順序即轉場順序）",
                show_label=True, columns=6, height=180,
                elem_classes=["shot-strip"],
            )

            def preview_uploads(files):
                if not files:
                    return []
                out = []
                for f in files:
                    p = f.name if hasattr(f, "name") else str(f)
                    if Path(p).exists():
                        out.append((p, Path(p).name))
                return out

            keyframe_files.change(preview_uploads, keyframe_files, keyframe_gallery)

            with gr.Row():
                kf_prompt = gr.Textbox(
                    label="轉場 prompt（單一描述，套用到每段）",
                    value="smooth cinematic transition",
                    lines=2,
                )
                kf_seg_dur = gr.Number(
                    value=4, label="每段轉場秒數", precision=1, minimum=1, maximum=10,
                )

            kf_gen_btn = gr.Button("開始生成轉場（N-1 段）",
                                    variant="primary", size="lg")
            kf_log_box = gr.Textbox(label="進度", lines=12, max_lines=20,
                                     autoscroll=True, elem_id="log_box")

            gr.Markdown(
                "提示：\n"
                "- 圖檔可用任何方法準備：手機照片、Midjourney、Stable Diffusion 都可\n"
                "- 兩張圖差異越大轉場越戲劇，但模型可能會編造不合理過渡\n"
                "- 解析度自動依「畫面比例」套用，圖會被中央裁切到目標尺寸"
            )

        # ============================ Tab 3: AI 靜圖 ==============================
        with gr.Tab("AI 靜圖（FLUX / Z-Image）"):
            gr.Markdown(
                "用本地 mflux 生成分鏡靜圖，無需 Midjourney 訂閱。\n\n"
                "**模型選擇**：\n"
                "- **z-image-turbo / z-image**（推薦）：開放權重，無需 HF 登入。"
                "z-image-turbo 約 8 步，速度與 FLUX.1-schnell 相當。\n"
                "- **FLUX.1-schnell / dev**：**需 HF 授權**。"
                "請先到 https://huggingface.co/black-forest-labs/FLUX.1-schnell "
                "點「Agree and access repository」，再執行 `huggingface-cli login`，"
                "重啟 UI 才能下載。匿名嘗試會 401 Gated。\n\n"
                "首次跑會下載對應權重，存到 `~/.cache/huggingface/hub/`。"
            )

            with gr.Row():
                flux_prompt = gr.Textbox(
                    label="圖片 prompt（英文效果通常較好）",
                    value="a serene Taiwanese fishing village at golden hour, "
                          "elderly fisherman silhouette, cinematic, photorealistic",
                    lines=3, scale=3,
                )
                with gr.Column(scale=1):
                    flux_model = gr.Dropdown(
                        list(FLUX_MODELS), value=list(FLUX_MODELS)[0],
                        label="模型",
                    )
                    flux_count = gr.Number(
                        value=3, label="生成張數", precision=0,
                        minimum=1, maximum=8,
                    )
                    flux_seed = gr.Number(
                        value=-1, label="Seed（-1 隨機，多張會自動 +1）",
                        precision=0,
                    )

            flux_gen_btn = gr.Button(
                "開始生成靜圖（依全域畫面比例）",
                variant="primary", size="lg",
            )
            flux_status = gr.Markdown("")
            flux_log_box = gr.Textbox(
                label="進度", lines=10, max_lines=18,
                autoscroll=True, elem_id="log_box",
            )
            flux_gallery = gr.Gallery(
                label="生成結果（點任一張可放大；右鍵複製路徑後貼到 Tab 1 / 2）",
                show_label=True, columns=4, height=320,
                elem_classes=["shot-strip"],
            )

            def update_flux_gallery(paths):
                return [(p, Path(p).name) for p in paths if Path(p).exists()]

            flux_gen_btn.click(
                stream_flux,
                [flux_prompt, flux_model, flux_count, aspect, flux_seed],
                [flux_log_box, flux_state, flux_status],
            ).then(update_flux_gallery, flux_state, flux_gallery)

            with gr.Row():
                push_to_t1_btn = gr.Button(
                    "把生成圖一鍵新增為「手動分鏡」列",
                    variant="primary",
                )
                push_to_t2_btn = gr.Button(
                    "把生成圖一鍵丟到「圖片轉場」",
                )
            push_to_t1_btn.click(
                push_flux_to_storyboard,
                [flux_state, storyboard, default_dur],
                storyboard,
            )
            push_to_t2_btn.click(
                lambda paths: [p for p in paths if Path(p).exists()],
                flux_state, keyframe_files,
            ).then(
                preview_uploads, keyframe_files, keyframe_gallery,
            )

            gr.Markdown(
                "**接續工作流**：\n"
                "1. 一鍵按鈕直接把生圖塞到 Tab 1 / 2，無需手動複製路徑\n"
                "2. 或右鍵 gallery 圖片 → 「複製圖片網址」拿本地路徑手動貼"
            )

    # ========================== 共用區：預覽 + 串接 ==========================
    gr.Markdown("## 完成鏡頭與最終串接")
    shot_gallery = gr.Gallery(
        label="完成鏡頭（依生成順序）",
        show_label=True, columns=4, height=240,
        elem_classes=["shot-strip"],
    )
    with gr.Row():
        concat_btn = gr.Button("串接成最終影片", variant="primary", scale=2)
        clear_completed_btn = gr.Button("清空完成清單", scale=1)
    final_video = gr.Video(label="最終串接影片", height=420)
    concat_status = gr.Markdown("")

    def update_gallery_from_state(shots):
        return [(p, Path(p).name) for p in shots if Path(p).exists()]

    def clear_completed():
        return [], [], None, "已清空完成清單"

    # 備份 story_text 內容到 State：使用者每次編輯 / scraper 寫入時同步
    story_text.change(lambda x: x, story_text, story_text_state)

    # tab 切換時把 State 還原回 story_text（修 Gradio 6.x lazy render 重置）
    tabs.select(lambda s: s, story_text_state, story_text)

    def _maybe_scrape_first(existing_story_text, url, mode_label, target):
        """智能 pre-step：若 story_text 空白且 URL 有值 → 從 v3ctor 抓回填；
        否則用現有 story_text。"""
        if existing_story_text and existing_story_text.strip():
            return existing_story_text, "使用手寫腳本"
        if not url or not url.strip():
            return "", "腳本與 URL 都空白 — 請至少填一個"
        try:
            article = scrape_v3ctor(url.strip())
        except Exception as e:
            return "", f"scrape 失敗：{type(e).__name__} {e}"
        mode = SPLIT_MODE_LABELS.get(mode_label, "智能")
        shots = split_to_shots(article["body"], mode, int(target))
        if not shots:
            return "", f"抓到《{article['title']}》但拆不出鏡頭"
        return (
            "\n".join(shots),
            f"自動抓取：《{article['title']}》→ {len(shots)} 鏡",
        )

    story_run_btn.click(
        _maybe_scrape_first,
        [story_text, v3_url, v3_split_mode, v3_target_chars],
        [story_text, v3_info],
    ).then(
        stream_story_pipeline,
        [story_text, story_style, story_custom_style, story_sec, story_motion,
         aspect, fps, seed, model, enhance, voice, burn_subtitle, mode,
         bgm_file, bgm_volume, i2v_mode_select, skip_image_gen],
        [story_log, completed_state, story_final_video, story_status],
    ).then(update_gallery_from_state, completed_state, shot_gallery)

    gen_btn.click(
        stream_generate,
        [storyboard, aspect, fps, mode, seed, model, enhance, voice, burn_subtitle,
         i2v_mode_select],
        [log_box, completed_state, final_video],
    ).then(update_gallery_from_state, completed_state, shot_gallery)

    kf_gen_btn.click(
        stream_keyframes,
        [keyframe_files, kf_prompt, kf_seg_dur, aspect, fps, seed, model],
        [kf_log_box, completed_state, final_video],
    ).then(update_gallery_from_state, completed_state, shot_gallery)

    concat_btn.click(
        concat_shots,
        [completed_state, bgm_file, bgm_volume],
        [final_video, concat_status],
    )
    clear_completed_btn.click(
        clear_completed, None,
        [completed_state, shot_gallery, final_video, concat_status],
    )

    # storyboard 的匯出/匯入需要在 storyboard 定義之後才能綁，所以放這裡（仍能存取它）
    export_btn.click(
        export_json,
        [storyboard, aspect, fps, mode, seed, model, enhance, voice, burn_subtitle],
        json_box,
    )
    import_btn.click(
        import_json, json_box,
        [storyboard, aspect, fps, mode, seed, model, enhance, voice, burn_subtitle],
    )

    gr.Markdown(
        "---\n"
        "**模式比較速查**\n\n"
        "| 模式 | 適合 | 速度 | 圖像忠實度 |\n"
        "|------|------|------|----------|\n"
        "| 分鏡腳本（無圖） | 純 t2v 多鏡頭 | 快 (~1 min) | — |\n"
        "| 分鏡腳本（每鏡一圖） | 有起手圖 + prompt 動作 | 中 (~2-3 min) | 中（單錨 → 雙錨改善） |\n"
        "| 關鍵幀串接 | 多張圖按順序轉場 | 中 (~3 min/段) | **高**（起終雙鎖）|\n\n"
        "兩個 tab 的輸出都會丟到上方「完成鏡頭」，串接時一起進去。"
    )


if __name__ == "__main__":
    if not LTX_BIN.exists():
        print(f"[警告] 找不到 {LTX_BIN}", file=sys.stderr)
        print("請先安裝 ltx-2-mlx（git clone + uv sync）。", file=sys.stderr)
    app.queue().launch(
        server_name="127.0.0.1", server_port=7860, inbrowser=True,
        theme=gr.themes.Soft(), css=CSS,
        # OUT_DIR 在 ~/ltx-2-mlx/output/，不在 cwd 內。
        # 不加 allowed_paths 的話 Gradio 會拒絕 serve mp4/png/aiff 給瀏覽器，
        # 結果就是「檔案有產出但 UI 顯示載入錯誤」。
        allowed_paths=[str(OUT_DIR)],
    )
