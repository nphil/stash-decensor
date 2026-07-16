"""Video upscaler for the compute runner.

Decodes with PyAV, upscales each frame through a spandrel-loaded SR model
(default: 2xLiveActionV1_SPAN — trained on live-action video degradations),
encodes to a *fragmented* mp4 (readable while growing -> live preview/feed
work), then muxes the source audio back in with ffmpeg.

fp32 on CUDA: correct on Pascal (P40 has no usable fp16) and fine everywhere
else. SPAN is tiny (~8.5 MB), so no tiling is needed even at 4K on 24 GB.

Progress lines mimic lada-cli's format so the runner's parser Just Works:
    upscaling:  42%| |Processed: 00:12 (1234f) | Remaining: 01:23 | Speed: 12.3f/s
"""
import os
import sys
import time
import argparse
import subprocess

import av
import torch
from spandrel import ModelLoader


def log(msg):
    print(msg, flush=True)


def hms(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--model", default=os.environ.get(
        "UPSCALE_MODEL", "/models/2xLiveActionV1_SPAN_490000.pth"))
    ap.add_argument("--encoder", default=os.environ.get("LADA_DEFAULT_ENCODER", "libx264"))
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    desc = ModelLoader().load_from_file(args.model)
    scale = getattr(desc, "scale", 2)
    model = desc.model.eval().float().to(args.device)
    log(f"model: {os.path.basename(args.model)} scale={scale}x device={args.device}")

    in_c = av.open(args.input)
    vs = in_c.streams.video[0]
    vs.thread_type = "AUTO"
    fps = vs.average_rate or 30
    total = vs.frames or 0
    if not total and vs.duration and vs.time_base:
        total = int(float(vs.duration * vs.time_base) * float(fps))
    w, h = vs.codec_context.width, vs.codec_context.height
    log(f"input: {w}x{h} @ {float(fps):.3f}fps ~{total} frames -> {w*scale}x{h*scale}")

    stem = os.path.splitext(os.path.basename(args.input))[0]
    video_tmp = os.path.join(args.output_dir, stem + ".upscaling.tmp.mp4")
    final = os.path.join(args.output_dir, stem + ".upscaled.mp4")

    out_c = av.open(video_tmp, "w", options={
        "movflags": "frag_keyframe+empty_moov+default_base_moof"})  # readable while growing
    enc = args.encoder or "libx264"
    try:
        ostream = out_c.add_stream(enc, rate=fps)
    except Exception as exc:  # noqa: BLE001 - encoder unavailable -> CPU fallback
        log(f"encoder {enc} unavailable ({exc}); falling back to libx264")
        enc = "libx264"
        ostream = out_c.add_stream(enc, rate=fps)
    ostream.width, ostream.height = w * scale, h * scale
    ostream.pix_fmt = "yuv420p"

    t0 = time.time()
    done = 0
    last_report = 0.0
    with torch.no_grad():
        for frame in in_c.decode(vs):
            img = frame.to_ndarray(format="rgb24")
            ten = torch.from_numpy(img).to(args.device).permute(2, 0, 1).unsqueeze(0).float().div_(255.0)
            out = model(ten)
            arr = out.clamp_(0, 1).mul_(255.0).round_().byte()[0].permute(1, 2, 0).cpu().numpy()
            nf = av.VideoFrame.from_ndarray(arr, format="rgb24")
            for pkt in ostream.encode(nf):
                out_c.mux(pkt)
            done += 1
            now = time.time()
            if now - last_report >= 2.0:
                last_report = now
                rate = done / max(0.001, now - t0)
                pct = int(100 * done / total) if total else 0
                remain = hms((total - done) / rate) if total and rate > 0 else "?"
                log(f"upscaling: {pct:3d}%| |Processed: {hms(done / float(fps))} ({done}f) | "
                    f"Remaining: {remain} | Speed: {rate:.1f}f/s")
    for pkt in ostream.encode():
        out_c.mux(pkt)
    out_c.close()
    in_c.close()
    rate = done / max(0.001, time.time() - t0)
    log(f"upscaled {done} frames in {hms(time.time() - t0)} ({rate:.1f}f/s); muxing audio")

    # mux original audio (if any) back in; -shortest guards tiny drift at EOF
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                        "-i", video_tmp, "-i", args.input,
                        "-map", "0:v", "-map", "1:a?",
                        "-c", "copy", "-shortest", final],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log("audio mux failed (" + (r.stderr or "").strip()[-150:] + "); keeping video-only output")
        os.replace(video_tmp, final)
    else:
        os.remove(video_tmp)
    log(f"done -> {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
