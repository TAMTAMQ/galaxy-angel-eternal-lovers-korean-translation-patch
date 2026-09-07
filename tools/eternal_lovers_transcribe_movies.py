from __future__ import annotations

import argparse
import json
from pathlib import Path

from faster_whisper import WhisperModel


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe Eternal Lovers movie PCM WAV files to Japanese JSON.")
    parser.add_argument("--wav-dir", type=Path, default=Path("movie/output"))
    parser.add_argument("--out-dir", type=Path, default=Path("movie/subtitles"))
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--only", action="append", default=[], help="Optional movie stem such as GADAT100; repeatable")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(args.model, device="cpu", compute_type="int8")

    wavs = sorted(args.wav_dir.glob("GADAT*_pcm.wav"))
    if args.only:
        wanted = set(args.only)
        wavs = [p for p in wavs if p.name.removesuffix("_pcm.wav") in wanted]

    for wav in wavs:
        stem = wav.name.removesuffix("_pcm.wav")
        segments_iter, info = model.transcribe(
            str(wav),
            language="ja",
            beam_size=5,
            best_of=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200},
            condition_on_previous_text=False,
            temperature=0.0,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
        )
        segments = []
        for seg in segments_iter:
            text = seg.text.strip()
            if not text:
                continue
            segments.append(
                {
                    "start": round(float(seg.start), 3),
                    "end": round(float(seg.end), 3),
                    "text": text,
                    "avg_logprob": round(float(seg.avg_logprob), 4),
                    "no_speech_prob": round(float(seg.no_speech_prob), 4),
                }
            )
        out = {
            "schema": "eternal-lovers-movie-transcript/v1",
            "source": str(wav),
            "language": info.language,
            "language_probability": float(info.language_probability),
            "duration": float(info.duration),
            "segments": segments,
        }
        out_path = args.out_dir / f"{stem}.ja.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{stem}: {len(segments)} segments, {info.duration:.2f}s -> {out_path}")


if __name__ == "__main__":
    main()
