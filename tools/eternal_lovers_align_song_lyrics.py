from __future__ import annotations

import argparse
import json
from pathlib import Path

from faster_whisper import WhisperModel


PROMPTS = {
    "GADAT101": """Angel's song 何度でも 翼ひろげ AH 宇宙に唄おう
戦う理由求めて 星座を紡いで走る
闇のソナタに惑う時 そう 崩す勇気
歴史の果てに 飛ぼう銀河 Shooting star
涙越えようよ 紋章に誓った 運命受け止めて
君がいてくれる だから Keep on smile 天使のSymphony
さあ 夜明けを奏でよう""",
    "GADAT137": """未来に咲く花の種がここにあるよ 君は空みたいな笑顔でそう呟いた
feel 星降る丘で hear 幾千の灯火 遥か銀河を見上げて想う 今ここにいる理由を
そう一つだけ一人だけ 守る事 出来ればいいんだよ
それは大きな 大きなHistory 創ってく確かな夢 伝えたいSerenade
笑顔のまま泣いてそれを綺麗と言う 君が優し過ぎて嬉しくてまた泣けたよ
wish どうかこのまま time 崩れないでいて 私といわれる全てのものは 君だけで溢れてる
そう言葉さえ心さえ 溶かしてく 繋いだぬくもり
それは涙を 涙さえも 虹にする陽の光 私の帰る場所
煌めいた銀河のように 生まれゆく命のように 素晴らしき夢の息吹きを
この空に星に虹に愛に 遥か瞬きになる
ここからまた Ah 始まる Angel's story
さあ二つなら二人なら 創れるよ未来のSymphony
終止符のない Eternal love song 歴史を奏でよう 星達を花束に 終わりなきPrelude""",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stem", choices=sorted(PROMPTS))
    parser.add_argument("--wav-dir", type=Path, default=Path("movie/output"))
    parser.add_argument("--out-dir", type=Path, default=Path("movie/subtitles"))
    args = parser.parse_args()

    wav = args.wav_dir / f"{args.stem}_pcm.wav"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    model = WhisperModel("large-v3", device="cpu", compute_type="int8")
    segments_iter, info = model.transcribe(
        str(wav),
        language="ja",
        beam_size=5,
        best_of=5,
        vad_filter=False,
        chunk_length=30,
        condition_on_previous_text=True,
        initial_prompt=PROMPTS[args.stem],
        temperature=0.0,
        no_speech_threshold=0.9,
        compression_ratio_threshold=2.8,
        log_prob_threshold=-1.2,
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
        "schema": "eternal-lovers-song-alignment/v1",
        "source": str(wav),
        "duration": float(info.duration),
        "segments": segments,
    }
    path = args.out_dir / f"{args.stem}.ja.songalign.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{args.stem}: {len(segments)} segments -> {path}")


if __name__ == "__main__":
    main()
