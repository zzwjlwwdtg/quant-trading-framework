from __future__ import annotations

import argparse
import json
import sys

from .interface import (
    format_jp_social_banner,
    get_jp_social_signal,
    get_jp_social_signal_cached,
)
from .settings import INBOX_DIR, SOURCE_CONFIG, SOURCE_CONFIG_EXAMPLE, ensure_dirs


def main() -> int:
    parser = argparse.ArgumentParser(description="Japan stock social recommendation scanner")
    parser.add_argument("--hours", type=int, default=168, help="lookback window, default one week")
    parser.add_argument("--max-items", type=int, default=80)
    parser.add_argument("--no-llm", action="store_true", help="use rule extractor only")
    parser.add_argument("--no-opend", action="store_true", help="skip moomoo OpenD quote verification")
    parser.add_argument("--fresh", action="store_true", help="ignore aggregate cache")
    parser.add_argument("--json", action="store_true", help="print full JSON")
    parser.add_argument("--where", action="store_true", help="print config and inbox paths")
    parser.add_argument("--probe-opend", metavar="CODE", help="probe one JP code through moomoo OpenD")
    parser.add_argument("--fetch-youtube", action="store_true", help="fetch latest YouTube RSS items into inbox before scanning")
    parser.add_argument("--fetch-max", type=int, default=5, help="max videos per YouTube channel for --fetch-youtube")
    parser.add_argument("--fetch-video", metavar="URL", help="fetch subtitles for one YouTube URL into inbox before scanning")
    parser.add_argument("--fetch-youtube-subs", action="store_true", help="fetch subtitles for recent configured YouTube videos")
    parser.add_argument("--list-subs", metavar="URL", help="diagnose available subtitle tracks for one YouTube URL")
    parser.add_argument("--creator", default="", help="creator id for --fetch-video")
    parser.add_argument("--sub-langs", default="ja,zh-Hans,zh-Hant,zh,en.*,en", help="subtitle language preference for yt-dlp")
    parser.add_argument("--cookies-from-browser", default="", help="pass browser cookies to yt-dlp, e.g. chrome, edge, firefox")
    parser.add_argument("--whisper-fallback", action="store_true", help="download audio and run local faster-whisper when captions are unavailable")
    parser.add_argument("--quality", choices=["fast", "balanced", "high"], default="balanced",
                        help="Whisper fallback quality preset: fast=tiny, balanced=small, high=medium")
    parser.add_argument("--whisper-model", default="", help="override faster-whisper model size, e.g. tiny/base/small/medium")
    parser.add_argument("--whisper-language", default="", help="force Whisper language, e.g. ja/zh/en; empty lets Whisper decide")
    parser.add_argument("--force-download", action="store_true", help="redownload subtitles/audio even if creator__videoid.jsonl exists")
    parser.add_argument("--no-cleanup", action="store_true", help="do not remove inbox files older than --hours")
    parser.add_argument("--report", action="store_true", help="write one-week creator summary as Markdown and PDF")
    parser.add_argument("--report-dir", default="", help="output directory for --report")
    parser.add_argument("--report-timeout", type=int, default=420, help="AI CLI timeout in seconds for report summary")
    parser.add_argument("--report-no-llm", action="store_true", help="use deterministic report template instead of AI CLI")
    parser.add_argument("--report-no-backtest", action="store_true", help="skip yfinance prediction-day close vs latest price checks")
    parser.add_argument("--report-md-only", action="store_true", help="write only Markdown for --report")
    parser.add_argument("--report-pdf-only", action="store_true", help="write only PDF for --report")
    args = parser.parse_args()

    ensure_dirs()
    if args.where:
        print(f"inbox:   {INBOX_DIR}")
        print(f"config:  {SOURCE_CONFIG}")
        print(f"example: {SOURCE_CONFIG_EXAMPLE}")
        return 0
    if args.probe_opend:
        from .opend import verify_with_opend
        res = verify_with_opend([args.probe_opend], timeout=6)
        for code, check in res.items():
            print(f"{code}: ok={check.ok} last_price={check.last_price} volume={check.volume} error={check.error}")
        return 0
    if args.list_subs:
        from .youtube_subtitles import list_available_subtitles
        out, status = list_available_subtitles(
            args.list_subs,
            cookies_from_browser=args.cookies_from_browser,
        )
        print(f"[list-subs] status={status}")
        print(out)
        return 0

    if not args.no_cleanup:
        from .sources import cleanup_inbox_items
        cleanup = cleanup_inbox_items(lookback_hours=args.hours)
        if cleanup.get("removed"):
            print(f"[cleanup] removed={len(cleanup['removed'])} cutoff={cleanup['cutoff']}")
            for name in cleanup["removed"][:12]:
                print(f"[cleanup] removed {name}")
    if args.fetch_youtube:
        from .youtube_rss import fetch_youtube_rss_to_inbox
        path, count, errors = fetch_youtube_rss_to_inbox(max_items_per_channel=args.fetch_max)
        print(f"[youtube] fetched_items={count} inbox_file={path or '-'}")
        for err in errors[:10]:
            print(f"[youtube] WARN {err}")
    if args.fetch_video:
        from .youtube_subtitles import fetch_video_subtitle_to_inbox
        path, status = fetch_video_subtitle_to_inbox(
            args.fetch_video,
            creator=args.creator,
            langs=args.sub_langs,
            cookies_from_browser=args.cookies_from_browser,
            whisper_fallback=args.whisper_fallback,
            whisper_model=args.whisper_model,
            whisper_language=args.whisper_language,
            quality=args.quality,
            force_download=args.force_download,
        )
        print(f"[video-subtitle] status={status} inbox_file={path or '-'}")
    if args.fetch_youtube_subs:
        from .youtube_subtitles import fetch_recent_channel_subtitles_to_inbox
        count, skipped, errors = fetch_recent_channel_subtitles_to_inbox(
            lookback_hours=args.hours,
            max_items_per_channel=args.fetch_max,
            langs=args.sub_langs,
            cookies_from_browser=args.cookies_from_browser,
            whisper_fallback=args.whisper_fallback,
            whisper_model=args.whisper_model,
            whisper_language=args.whisper_language,
            quality=args.quality,
        )
        print(f"[channel-subtitles] fetched_files={count} skipped_existing={skipped}")
        for err in errors[:20]:
            print(f"[channel-subtitles] WARN {err}")

    if args.fresh:
        sig = get_jp_social_signal(
            lookback_hours=args.hours,
            max_items=args.max_items,
            use_llm=not args.no_llm,
            verify_opend=not args.no_opend,
        )
    else:
        sig = get_jp_social_signal_cached(
            lookback_hours=args.hours,
            max_items=args.max_items,
            use_llm=not args.no_llm,
            verify_opend=not args.no_opend,
        )

    if args.report:
        from .weekly_report import write_weekly_report
        write_md = not args.report_pdf_only
        write_pdf = not args.report_md_only
        report = write_weekly_report(
            sig=sig,
            lookback_hours=args.hours,
            max_items=args.max_items,
            output_dir=args.report_dir or None,
            use_llm=not args.report_no_llm,
            llm_timeout=args.report_timeout,
            include_backtest=not args.report_no_backtest,
            write_md=write_md,
            write_pdf=write_pdf,
        )
        print(f"[report] llm_status={report.get('report_llm_status')}")
        if report.get("markdown_path"):
            print(f"[report] md={report['markdown_path']}")
        if report.get("pdf_path"):
            print(f"[report] pdf={report['pdf_path']}")
        print(f"[report] payload={report['payload_path']}")

    if args.json:
        print(json.dumps(sig, ensure_ascii=False, indent=2))
    else:
        for line in format_jp_social_banner(sig):
            print(line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
