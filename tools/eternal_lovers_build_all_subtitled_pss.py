from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from galaxy_angel_build_subtitled_pss import parse_pictures
from galaxy_angel_pssplex_mux import frame_rate_from_es, mux, template_timing

EXPECTED_MOVIES = 39
EXPECTED_SUBTITLED_MOVIES = 24


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(16 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def get_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found

    known = Path.home() / "AppData/Local/ArNosurgeKoreanPatch/ffmpeg/ffmpeg.exe"
    if known.is_file():
        return str(known)

    repo_ffmpeg = Path(__file__).resolve().parents[3] / "vendor/ffmpeg/ffmpeg-9.0.1-essentials_build/bin/ffmpeg.exe"
    if repo_ffmpeg.is_file():
        return str(repo_ffmpeg)

    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("ffmpeg is unavailable") from exc


def source_average_bitrate_kbps(source_es: bytes, frame_rate: Fraction, picture_count: int) -> int:
    if picture_count <= 0:
        raise ValueError("cannot determine source bitrate without pictures")
    duration = picture_count / float(frame_rate)
    return max(1, round((len(source_es) * 8) / duration / 1000))


def encode_m2v(
    ffmpeg: str,
    source: Path,
    subtitle: Path,
    output: Path,
    movie_root: Path,
    frame_rate: Fraction,
    bitrate_kbps: int,
    maxrate_kbps: int,
    vbv_bytes: int,
    match_source_bitrate: bool,
) -> list[str]:
    src_arg = source.resolve().relative_to(movie_root.resolve()).as_posix()
    sub_arg = subtitle.resolve().relative_to(movie_root.resolve()).as_posix()
    out_arg = output.resolve().relative_to(movie_root.resolve()).as_posix()
    rate_arg = (
        str(frame_rate.numerator)
        if frame_rate.denominator == 1
        else f"{frame_rate.numerator}/{frame_rate.denominator}"
    )
    gop = max(12, round(float(frame_rate) / 2))
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        src_arg,
        "-map",
        "0:v:0",
        "-vf",
        f"ass={sub_arg}",
        "-an",
        "-c:v",
        "mpeg2video",
        "-pix_fmt",
        "yuv420p",
        "-r",
        rate_arg,
        "-aspect",
        "4:3",
        "-g",
        str(gop),
        "-bf",
        "2",
        "-b:v",
        f"{bitrate_kbps}k",
    ]
    # Eternal Lovers' source M2V files are effectively CBR: the declared
    # sequence-header rate is also their real average rate.  PS2STR schedules
    # SCR from that declared rate, so allowing the subtitle encode to fall
    # materially below it makes the whole movie arrive seconds too early.
    # Keep min/target/max together for both fixed and source-matched modes.
    cmd.extend(["-minrate", f"{bitrate_kbps}k"])
    cmd.extend([
        "-maxrate",
        f"{maxrate_kbps}k",
        "-bufsize",
        str(vbv_bytes),
        "-qmin",
        "2",
        "-qmax",
        "31",
        "-trellis",
        "1",
        "-f",
        "mpeg2video",
        out_arg,
    ])
    subprocess.run(cmd, cwd=movie_root, check=True)
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build Eternal Lovers subtitle-burned PSS with the proven 7.536 Mbps PSS mux layout and paced audio interleave."
    )
    ap.add_argument("--movie-root", type=Path, default=Path("movie"))
    ap.add_argument("--output-dir", type=Path, default=Path("movie/subtitled/final"))
    ap.add_argument("--report", type=Path, default=Path("build/eternal_lovers_subtitled_pss_report.json"))
    ap.add_argument("--bitrate-kbps", type=int, default=5950)
    ap.add_argument("--maxrate-kbps", type=int, default=6000,
                    help="Fixed-mode maxrate; 6000 reproduces the proven ga_ml_5950 test")
    ap.add_argument("--vbv-bytes", type=int, default=1835008)
    ap.add_argument("--names", nargs="*")
    ap.add_argument("--skip-encode", action="store_true")
    ap.add_argument("--nonempty-only", action="store_true",
                    help="Build only movies whose Korean ASS contains Dialogue events")
    ap.add_argument("--match-source-bitrate", action="store_true",
                    help="Match each source M2V average bitrate instead of forcing the bitrate cap")
    ap.add_argument("--bitrate-scale", type=float, default=1.0,
                    help="Scale source-average bitrate before applying --bitrate-kbps as a cap")
    ap.add_argument("--audio-prefetch-slots", type=int, default=0,
                    help="Schedule audio this many 4 KiB slots earlier inside the normal PSS mux")
    ap.add_argument("--audio-burst-packets", type=int, default=2,
                    help="Emit original-like consecutive private-audio packets per burst (default: 2)")
    ap.add_argument("--use-original-base-pts", action="store_true",
                    help="Use each original PSS first video PTS as the common starting PTS")
    ap.add_argument("--audio-target-lead-ms", type=float,
                    help="Target audio PTS lead over pack SCR before burst scheduling")
    ap.add_argument("--use-original-mux-rate", action="store_true",
                    help="Experimental: use the source PSS mux-rate/rate-bound instead of the proven 7.536 Mbps mux timing")
    args = ap.parse_args()

    root = args.movie_root
    originals = root / "original"
    extracted = root / "output"
    subtitles = root / "subtitles"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    names = args.names or sorted(p.stem for p in originals.glob("GADAT*.PSS"))
    if not args.names and len(names) != EXPECTED_MOVIES:
        raise ValueError(f"expected {EXPECTED_MOVIES} original PSS files, found {len(names)}")
    if args.nonempty_only:
        names = [
            name for name in names
            if any(
                line.startswith("Dialogue:")
                for line in (subtitles / f"{name}.ko.ass").read_text(encoding="utf-8").splitlines()
            )
        ]
        if len(names) != EXPECTED_SUBTITLED_MOVIES:
            raise ValueError(
                f"expected {EXPECTED_SUBTITLED_MOVIES} non-empty subtitle movies, found {len(names)}"
            )

    ffmpeg = get_ffmpeg()
    results: list[dict[str, object]] = []

    for index, name in enumerate(names, 1):
        original_pss = originals / f"{name}.PSS"
        source_m2v = extracted / f"{name}.m2v"
        wav = extracted / f"{name}_pcm.wav"
        subtitle = subtitles / f"{name}.ko.ass"
        encoded_m2v = args.output_dir / f"{name}.m2v"
        pss = args.output_dir / f"{name}.PSS"
        pss_report = args.output_dir / f"{name}.pss.json"

        missing = [str(p) for p in (original_pss, source_m2v, wav, subtitle) if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"{name}: missing inputs: {missing}")

        source_es = source_m2v.read_bytes()
        frame_rate = frame_rate_from_es(source_es)
        source_pictures = parse_pictures(source_es)
        source_bitrate_kbps = source_average_bitrate_kbps(source_es, frame_rate, len(source_pictures))
        encode_bitrate_kbps = args.bitrate_kbps
        encode_maxrate_kbps = args.maxrate_kbps
        if args.match_source_bitrate:
            encode_bitrate_kbps = min(
                args.bitrate_kbps,
                max(1, round(source_bitrate_kbps * args.bitrate_scale)),
            )
            # PS2STR derives the PSS program-stream rate from the MPEG-2
            # sequence-header bitrate plus the 1536 kbps PCM stream.  Keeping
            # a 6000 kbps maxrate here made PS2STR choose 7536 kbps even when
            # Eternal Lovers' source movie is only ~4 Mbps, causing SCR to run
            # far ahead of the 92-second presentation timeline.  Match the
            # source movie's declared/average rate instead.
            encode_maxrate_kbps = encode_bitrate_kbps
        print(
            f"[{index}/{len(names)}] {name}: encode {frame_rate.numerator}/{frame_rate.denominator} fps, "
            f"pictures={len(source_pictures)}, source_avg={source_bitrate_kbps}k, "
            f"target={encode_bitrate_kbps}k, maxrate={encode_maxrate_kbps}k",
            flush=True,
        )

        encode_cmd = None
        if not args.skip_encode:
            encode_cmd = encode_m2v(
                ffmpeg,
                source_m2v,
                subtitle,
                encoded_m2v,
                root,
                frame_rate,
                encode_bitrate_kbps,
                encode_maxrate_kbps,
                args.vbv_bytes,
                args.match_source_bitrate,
            )
        if not encoded_m2v.is_file():
            raise FileNotFoundError(f"{name}: encoded M2V missing: {encoded_m2v}")

        encoded_es = encoded_m2v.read_bytes()
        encoded_pictures = parse_pictures(encoded_es)
        if len(encoded_pictures) != len(source_pictures):
            raise ValueError(
                f"{name}: picture count mismatch: source={len(source_pictures)} encoded={len(encoded_pictures)}"
            )

        mux_mode = "original mux-rate/rate-bound" if args.use_original_mux_rate else "proven 7.536 Mbps mux timing"
        print(f"[{index}/{len(names)}] {name}: PSS mux ({mux_mode}, paced audio interleave)", flush=True)
        original_base_pts = None
        if args.use_original_base_pts:
            timing = template_timing(original_pss)
            original_base_pts = int(timing["first_video_pts"])

        mux_report = mux(
            encoded_m2v,
            wav,
            pss,
            mux_template_pss=original_pss if args.use_original_mux_rate else None,
            audio_prefetch_slots=args.audio_prefetch_slots,
            audio_burst_packets=args.audio_burst_packets,
            base_pts_override=original_base_pts,
            audio_target_lead_ms=args.audio_target_lead_ms,
        )
        pss_report.write_text(json.dumps(mux_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        result = {
            "name": name,
            "original_pss": str(original_pss.resolve()),
            "source_m2v": str(source_m2v.resolve()),
            "subtitle": str(subtitle.resolve()),
            "wav": str(wav.resolve()),
            "encoded_m2v": str(encoded_m2v.resolve()),
            "pss": str(pss.resolve()),
            "original_pss_bytes": original_pss.stat().st_size,
            "pss_bytes": pss.stat().st_size,
            "size_delta": pss.stat().st_size - original_pss.stat().st_size,
            "frame_rate": f"{frame_rate.numerator}/{frame_rate.denominator}",
            "source_average_bitrate_kbps": source_bitrate_kbps,
            "encode_bitrate_kbps": encode_bitrate_kbps,
            "source_pictures": len(source_pictures),
            "encoded_pictures": len(encoded_pictures),
            "source_m2v_sha256": sha256_file(source_m2v),
            "encoded_m2v_sha256": sha256_file(encoded_m2v),
            "pss_sha256": sha256_file(pss),
            "encode_command": encode_cmd,
            "mux_verification": mux_report["verification"],
        }
        results.append(result)
        args.report.write_text(
            json.dumps(
                {
                    "schema": "eternal-lovers-all-subtitled-pss/v1",
                    "bitrate_kbps_cap": args.bitrate_kbps,
                    "match_source_bitrate": args.match_source_bitrate,
                    "bitrate_scale": args.bitrate_scale,
                    "vbv_bytes": args.vbv_bytes,
                    "audio_prefetch_slots": args.audio_prefetch_slots,
                    "audio_burst_packets": args.audio_burst_packets,
                    "use_original_base_pts": args.use_original_base_pts,
                    "audio_target_lead_ms": args.audio_target_lead_ms,
                    "use_original_mux_rate": args.use_original_mux_rate,
                    "completed": len(results),
                    "requested": len(names),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            f"[{index}/{len(names)}] {name}: OK pss={pss.stat().st_size} delta={result['size_delta']}",
            flush=True,
        )

    print(f"Completed {len(results)}/{len(names)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
