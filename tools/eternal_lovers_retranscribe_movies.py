from __future__ import annotations

import argparse
import json
from pathlib import Path

from faster_whisper import WhisperModel


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent-chunk second-pass transcription for Eternal Lovers movie WAV files.")
    parser.add_argument("--wav-dir", type=Path, default=Path("movie/output"))
    parser.add_argument("--out-dir", type=Path, default=Path("movie/subtitles"))
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--only", action="append", default=[], help="Movie stem such as GADAT110; repeatable")
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
            vad_filter=False,
            chunk_length=10,
            condition_on_previous_text=False,
            temperature=0.0,
            no_speech_threshold=0.72,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            hallucination_silence_threshold=2.0,
            word_timestamps=True,
        )
        segments = []
        for seg in segments_iter:
            text = seg.text.strip()
            if not text:
                continue
            words = []
            if seg.words:
                for word in seg.words:
                    words.append({
                        "start": None if word.start is None else round(float(word.start), 3),
                        "end": None if word.end is None else round(float(word.end), 3),
                        "word": word.word,
                        "probability": round(float(word.probability), 4),
                    })
            segments.append({
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": text,
                "avg_logprob": round(float(seg.avg_logprob), 4),
                "no_speech_prob": round(float(seg.no_speech_prob), 4),
                "words": words,
            })
        out = {
            "schema": "eternal-lovers-movie-transcript-pass2/v1",
            "source": str(wav),
            "model": args.model,
            "language": info.language,
            "language_probability": float(info.language_probability),
            "duration": float(info.duration),
            "segments": segments,
        }
        out_path = args.out_dir / f"{stem}.ja.pass2.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{stem}: {len(segments)} segments, {info.duration:.2f}s -> {out_path}")


if __name__ == "__main__":
    main()
