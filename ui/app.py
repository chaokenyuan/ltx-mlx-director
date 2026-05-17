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
                       voice: str, burn_subtitle: bool) -> tuple[Path, str]:
    """為單鏡頭做 TTS + 字幕後處理。

    回傳 (最終影片路徑, log 訊息)。
    若沒有旁白也沒燒字幕，直接回原檔。
    """
    if not narration or not narration.strip():
        return shot_video, "(無旁白，跳過後處理)"

    tmpdir = shot_video.parent
    base = shot_video.stem
    aiff = tmpdir / f"{base}.aiff"
    srt = tmpdir / f"{base}.srt"
    final = tmpdir / f"{base}_av.mp4"

    msgs = []
    audio_ok = tts_to_aiff(narration, voice, aiff)
    if audio_ok:
        msgs.append(f"TTS({voice})")
    else:
        msgs.append("TTS 失敗")

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
    "schnell (快, 4 steps)": ("schnell", 4),
    "dev (慢, 25 steps，更精細)": ("dev", 25),
}


def build_flux_cmd(prompt: str, model_key: str, aspect: str,
                   seed: int, out_path: Path) -> list[str]:
    """構建 mflux-generate 命令。"""
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
        "--quantize", "4",
        "--low-ram",
        "--output", str(out_path),
    ]
    return cmd


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
            log_lines.append(f"   -> 失敗 exit={p.returncode}")
        yield "\n".join(log_lines[-30:]), produced, f"已產出 {len(produced)} 張"

    progress(1.0, desc="完成")
    log_lines.append(f"\n全部完成。{len(produced)} / {count} 張")
    yield "\n".join(log_lines[-30:]), produced, f"完成：{len(produced)} 張"


def build_cmd(prompt: str, seconds: float, image: str, aspect: str, fps: int,
              mode_label: str, seed: int, model: str, enhance: bool,
              out_path: Path) -> tuple[list[str], bool]:
    """構建 ltx-2-mlx generate 命令。

    回傳 (cmd, used_i2v_lock)，後者用於 UI 顯示「i2v 強鎖定」狀態。

    參考圖識別保留策略：
    當提供 --image 時，原本 --distilled 模式因為 no CFG，圖只在第 0 幀短暫出現後
    立刻飄走（LTX-2.3 上游已知特性）。改用 --one-stage（含 CFG，q4 模型支援），
    並加上頭尾雙錨：--image PATH 0 1.0 + --image PATH (frames-1) 1.0，
    可大幅提升整支影片對參考圖的忠實度。
    """
    w, h = ASPECT_WH[aspect]
    frames = duration_to_frames(seconds, fps)

    has_image = bool(image and image.strip()
                     and Path(image).expanduser().exists())
    # 注意：--one-stage 不支援多錨點 i2v（只能單張單錨），所以有圖時改用 --two-stage
    # 雖然 help 寫 --two-stage 「requires q8」，實測 q4 也可正常執行。
    pipe = "--two-stage" if has_image else MODE_FLAGS[mode_label]

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
    if has_image:
        img_path = str(Path(image).expanduser())
        # 雙端錨點：第 0 幀 + 最後 1 幀皆鎖在同一張參考圖
        cmd += ["--image", img_path, "0", "1.0"]
        cmd += ["--image", img_path, str(frames - 1), "1.0"]
    if enhance:
        cmd += ["--enhance-prompt"]
    return cmd, has_image


def stream_generate(df: pd.DataFrame, aspect, fps, mode_label, seed, model,
                    enhance, voice, burn_subtitle, progress=gr.Progress()):
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
        cmd, i2v_lock = build_cmd(prompt, seconds, image, aspect, int(fps),
                                   mode_label, int(seed), model, enhance, out_path)

        progress((i) / total, desc=f"Shot {i+1}/{total}: {prompt[:30]}")
        lock_tag = " | i2v鎖定(two-stage+雙端錨)" if i2v_lock else ""
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


def concat_shots(shot_paths: list[str]):
    """串接已生成的鏡頭。

    Bug 修復：原本用 check=True，ffmpeg 在 concat demuxer + -c copy 模式下，
    遇到輕微的 timebase / DTS 警告會回傳非零但仍產出有效檔案，造成 UI 誤報失敗。
    改為「檔案存在且 > 0 byte」即視為成功；stream copy 真的失敗時降級為重編碼。
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
    if final_path.exists() and final_path.stat().st_size > 0:
        suffix = "" if proc.returncode == 0 else f"（ffmpeg 警告 rc={proc.returncode}，檔案仍有效）"
        return str(final_path), f"串接完成: {final_path.name}{suffix}"

    proc2 = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(final_path)],
        capture_output=True, text=True,
    )
    if final_path.exists() and final_path.stat().st_size > 0:
        return str(final_path), f"串接完成（已重新編碼）: {final_path.name}"
    err = (proc2.stderr or proc.stderr or "")[-400:]
    return None, f"ffmpeg 失敗: {err}"


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
            value=True, label="燒入字幕（中文 PingFang，旁白文字 → 字幕）",
        )

    completed_state = gr.State([])
    flux_state = gr.State([])

    with gr.Tabs():
        # ============================== Tab 1: 分鏡腳本 ==============================
        with gr.Tab("分鏡腳本（每鏡一圖 i2v）"):
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

        # ============================ Tab 2: 關鍵幀串接 ==============================
        with gr.Tab("關鍵幀串接（多圖轉場）"):
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

        # ============================ Tab 3: AI 靜圖生成 ==============================
        with gr.Tab("AI 靜圖生成（mflux）"):
            gr.Markdown(
                "用本地 **FLUX.1**（透過 `mflux`）生成分鏡靜圖，無需 Midjourney "
                "訂閱。圖會存到 `~/ltx-2-mlx/output/`，可直接拖到 Tab 1 i2v 區或 "
                "Tab 2 關鍵幀串接區。\n\n"
                "首次跑會下載 FLUX.1 權重（schnell ~6GB / dev ~12GB，4-bit 量化版）。"
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

            gr.Markdown(
                "**接續工作流**：\n"
                "1. 右鍵 gallery 圖片 → 「複製圖片網址」可得本地路徑\n"
                "2. 切到「分鏡腳本」Tab → 上傳該檔案到「參考圖」區 → 套用到列\n"
                "3. 或切到「關鍵幀串接」Tab → 直接把多張圖一起拖上去"
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

    # 兩個 generate 入口都更新 completed_state，並都會觸發 gallery 重繪
    gen_btn.click(
        stream_generate,
        [storyboard, aspect, fps, mode, seed, model, enhance, voice, burn_subtitle],
        [log_box, completed_state, final_video],
    ).then(update_gallery_from_state, completed_state, shot_gallery)

    kf_gen_btn.click(
        stream_keyframes,
        [keyframe_files, kf_prompt, kf_seg_dur, aspect, fps, seed, model],
        [kf_log_box, completed_state, final_video],
    ).then(update_gallery_from_state, completed_state, shot_gallery)

    concat_btn.click(concat_shots, completed_state, [final_video, concat_status])
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
    )
