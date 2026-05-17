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


def make_initial_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            [3, "廣角航拍：海邊夕陽，金色波光", ""],
            [5, "推近：礁石上的海鳥拍翅起飛，慢動作", ""],
            [2, "特寫：浪花拍上岩石碎成水霧", ""],
        ],
        columns=["秒", "Prompt", "i2v 圖片路徑(選填)"],
    )


def build_cmd(prompt: str, seconds: float, image: str, aspect: str, fps: int,
              mode_label: str, seed: int, model: str, enhance: bool,
              out_path: Path) -> list[str]:
    w, h = ASPECT_WH[aspect]
    frames = duration_to_frames(seconds, fps)
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
    if image and image.strip() and Path(image).expanduser().exists():
        cmd += ["--image", str(Path(image).expanduser())]
    if enhance:
        cmd += ["--enhance-prompt"]
    return cmd


def stream_generate(df: pd.DataFrame, aspect, fps, mode_label, seed, model,
                    enhance, progress=gr.Progress()):
    """逐鏡呼叫 ltx-2-mlx，stdout 串流回 UI。"""
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
            image = str(row["i2v 圖片路徑(選填)"] or "").strip()
        except Exception as e:
            log_lines.append(f"[{i+1}] 無效列，跳過: {e}")
            yield "\n".join(log_lines[-30:]), shots_done, None
            continue
        if not prompt:
            log_lines.append(f"[{i+1}] 空 prompt，跳過")
            yield "\n".join(log_lines[-30:]), shots_done, None
            continue

        out_path = OUT_DIR / f"{base}_{int(i)+1:02d}.mp4"
        cmd = build_cmd(prompt, seconds, image, aspect, int(fps),
                        mode_label, int(seed), model, enhance, out_path)

        progress((i) / total, desc=f"Shot {i+1}/{total}: {prompt[:30]}")
        log_lines.append(f"\n=== Shot {i+1}/{total} | {seconds}s | {aspect} ===")
        log_lines.append(f"Prompt: {prompt}")
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

    progress(1.0, desc="所有鏡頭完成")
    log_lines.append(f"\n全部完成。{len(shots_done)} / {total} 鏡頭成功。")
    yield "\n".join(log_lines[-30:]), shots_done, None


def concat_shots(shot_paths: list[str]):
    if not shot_paths:
        return None, "尚無已生成的鏡頭"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    list_file = OUT_DIR / f"final-{timestamp}.concat.txt"
    final_path = OUT_DIR / f"final-{timestamp}.mp4"
    list_file.write_text("\n".join(f"file '{p}'" for p in shot_paths))
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
             "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-c", "copy", str(final_path)],
            check=True, capture_output=True, text=True,
        )
        return str(final_path), f"串接完成: {final_path}"
    except subprocess.CalledProcessError as e:
        return None, f"ffmpeg 失敗: {e.stderr}"


def export_json(df: pd.DataFrame, aspect, fps, mode_label, seed, model, enhance):
    if df is None:
        df = make_initial_df()
    shots = [
        {"duration": float(r["秒"]), "prompt": str(r["Prompt"]),
         "image": str(r["i2v 圖片路徑(選填)"] or "")}
        for _, r in df.iterrows()
    ]
    data = {
        "settings": {
            "aspect": aspect, "fps": int(fps), "mode": mode_label,
            "seed": int(seed), "model": model, "enhance": bool(enhance),
        },
        "shots": shots,
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def import_json(text: str):
    if not text or not text.strip():
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()
    data = json.loads(text)
    s = data.get("settings", {})
    shots = data.get("shots", [])
    df = pd.DataFrame(
        [[sh.get("duration", 4), sh.get("prompt", ""), sh.get("image", "")]
         for sh in shots],
        columns=["秒", "Prompt", "i2v 圖片路徑(選填)"],
    )
    return (
        df,
        s.get("aspect", "16:9"),
        s.get("fps", 24),
        s.get("mode", list(MODE_FLAGS)[0]),
        s.get("seed", -1),
        s.get("model", "dgrauet/ltx-2.3-mlx-q4"),
        s.get("enhance", False),
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
    new = pd.DataFrame([[default_dur, "", ""]], columns=df.columns)
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

with gr.Blocks(title="LTX-2.3 Director", css=CSS, theme=gr.themes.Soft()) as app:
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

    gr.Markdown("## 分鏡腳本\n點選列以選取，再用按鈕移動或刪除。可以直接編輯儲存格。")

    storyboard = gr.Dataframe(
        value=make_initial_df(),
        headers=["秒", "Prompt", "i2v 圖片路徑(選填)"],
        datatype=["number", "str", "str"],
        row_count=(1, "dynamic"),
        col_count=(3, "fixed"),
        interactive=True,
        wrap=True,
        label="storyboard",
    )
    selected_row = gr.State(None)

    def on_select(evt: gr.SelectData):
        return int(evt.index[0]) if evt and evt.index is not None else None

    storyboard.select(on_select, outputs=selected_row)

    with gr.Row():
        add_btn = gr.Button("新增鏡頭")
        del_btn = gr.Button("刪除選取鏡頭", variant="stop")
        up_btn = gr.Button("選取列上移")
        dn_btn = gr.Button("選取列下移")

    add_btn.click(add_row, [storyboard, default_dur], storyboard)
    del_btn.click(delete_row, [storyboard, selected_row], storyboard)
    up_btn.click(lambda df, sel: move_row(df, sel, -1), [storyboard, selected_row], storyboard)
    dn_btn.click(lambda df, sel: move_row(df, sel, +1), [storyboard, selected_row], storyboard)

    gr.Markdown("## 生成")
    with gr.Row():
        gen_btn = gr.Button("開始生成全部鏡頭", variant="primary", scale=2)
        concat_btn = gr.Button("串接最終影片", scale=1)

    log_box = gr.Textbox(label="進度", lines=14, max_lines=20,
                          autoscroll=True, elem_id="log_box")

    completed_state = gr.State([])

    gr.Markdown("## 預覽")
    shot_gallery = gr.Gallery(label="完成鏡頭",
                                show_label=True, columns=4, height=240,
                                elem_classes=["shot-strip"])
    final_video = gr.Video(label="最終串接影片", height=420)
    concat_status = gr.Markdown("")

    def update_gallery_from_state(shots):
        return [(p, Path(p).name) for p in shots if Path(p).exists()]

    gen_btn.click(
        stream_generate,
        [storyboard, aspect, fps, mode, seed, model, enhance],
        [log_box, completed_state, final_video],
    ).then(
        update_gallery_from_state, completed_state, shot_gallery
    )

    concat_btn.click(concat_shots, completed_state, [final_video, concat_status])

    gr.Markdown("## 匯出 / 匯入 storyboard")
    with gr.Row():
        export_btn = gr.Button("匯出為 JSON")
        import_btn = gr.Button("從下方 JSON 匯入")
    json_box = gr.Code(label="Storyboard JSON", language="json", lines=12)

    export_btn.click(export_json,
                      [storyboard, aspect, fps, mode, seed, model, enhance],
                      json_box)
    import_btn.click(import_json, json_box,
                      [storyboard, aspect, fps, mode, seed, model, enhance])

    gr.Markdown(
        "---\n"
        "提示：\n"
        "- 首次跑 fast / hq 各會下載對應的權重檔（fast 約 12GB、hq 約 25GB+）。\n"
        "- 解析度依比例自動套用（皆為 32 倍數，貼近 LTX 訓練尺寸）。\n"
        "- frames 依秒數對齊到 `1 + 8N`，所以實際長度可能比設定多 0.x 秒。\n"
    )


if __name__ == "__main__":
    if not LTX_BIN.exists():
        print(f"[警告] 找不到 {LTX_BIN}", file=sys.stderr)
        print("請先安裝 ltx-2-mlx（git clone + uv sync）。", file=sys.stderr)
    app.queue().launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)
