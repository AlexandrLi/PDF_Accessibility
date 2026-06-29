"""Tag-preserving worksheet wrap via Coherent PDF (cpdf)."""

from __future__ import annotations

import platform
import subprocess
import tempfile
from pathlib import Path


class CpdfError(RuntimeError):
    pass


def resolve_cpdf_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        path = explicit
    else:
        import os

        env_path = os.environ.get("CPDF_PATH")
        if env_path:
            path = Path(env_path)
        else:
            repo_root = Path(__file__).resolve().parents[2]
            bundle_root = repo_root / "spike" / "cpdf-wrap" / "cpdf-binaries-master"
            machine = platform.machine().lower()
            if "arm" in machine or machine == "aarch64":
                path = bundle_root / "MacOS-ARM" / "cpdf"
            elif platform.system() == "Darwin":
                path = bundle_root / "MacOS-Intel" / "cpdf"
            else:
                path = bundle_root / "Linux-Intel-64bit" / "cpdf"

    if not path.is_file():
        raise CpdfError(
            f"cpdf not found at {path}. Install cpdf, set CPDF_PATH, or download "
            "https://github.com/coherentgraphics/cpdf-binaries"
        )
    if not path.stat().st_mode & 0o111:
        path.chmod(path.stat().st_mode | 0o111)
    return path


def resolve_cover_pdf_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        path = explicit
    else:
        import os

        env_path = os.environ.get("WORKSHEET_COVER_PDF")
        repo_root = Path(__file__).resolve().parents[2]
        candidates: list[Path] = []
        if env_path:
            candidates.append(Path(env_path))
        candidates.extend(
            [
                repo_root / "scripts" / "assets" / "cover.pdf",
                repo_root.parent / "generate-pdf-lambda" / "src" / "assets" / "cover_study_prep.pdf",
                repo_root.parent / "generate-pdf-lambda" / "src" / "assets" / "cover.pdf",
            ]
        )
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise CpdfError(
                "Worksheet cover PDF not found. Set WORKSHEET_COVER_PDF or place cover.pdf "
                "under scripts/assets/."
            )
    return path


def _run_cpdf(cpdf: Path, args: list[str]) -> None:
    result = subprocess.run(
        [str(cpdf), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise CpdfError(f"cpdf failed ({result.returncode}): {detail}")


def wrap_topic_download(
    preview_bytes: bytes,
    topic_title: str,
    *,
    cpdf: Path | None = None,
    cover: Path | None = None,
) -> bytes:
    cpdf_path = resolve_cpdf_path(cpdf)
    cover_path = resolve_cover_pdf_path(cover)

    with tempfile.TemporaryDirectory(prefix="cpdf-topic-") as tmp_dir:
        tmp = Path(tmp_dir)
        preview_path = tmp / "preview.pdf"
        merged_path = tmp / "merged.pdf"
        header_path = tmp / "header.pdf"
        output_path = tmp / "download.pdf"
        preview_path.write_bytes(preview_bytes)

        _run_cpdf(
            cpdf_path,
            [
                "-merge",
                str(cover_path),
                str(preview_path),
                "-process-struct-trees",
                "-subformat",
                "PDF/UA-2",
                "-o",
                str(merged_path),
            ],
        )
        _run_cpdf(
            cpdf_path,
            [
                "-add-text",
                topic_title,
                "-font",
                "Helvetica",
                "-font-size",
                "12",
                "-process-struct-trees",
                "-topleft",
                "35 40",
                str(merged_path),
                "2-end",
                "-o",
                str(header_path),
            ],
        )
        _run_cpdf(
            cpdf_path,
            [
                "-add-text",
                "Page %Page",
                "-font",
                "Helvetica",
                "-font-size",
                "10",
                "-process-struct-trees",
                "-bottomright",
                "70 15",
                str(header_path),
                "2-end",
                "-o",
                str(output_path),
            ],
        )
        return output_path.read_bytes()


def wrap_chapter_worksheet(
    preview_bytes_list: list[bytes],
    chapter_title: str,
    textbook_header: str,
    *,
    cpdf: Path | None = None,
    cover: Path | None = None,
) -> bytes:
    if not preview_bytes_list:
        raise CpdfError("Chapter wrap requires at least one preview PDF")

    cpdf_path = resolve_cpdf_path(cpdf)
    cover_path = resolve_cover_pdf_path(cover)

    with tempfile.TemporaryDirectory(prefix="cpdf-chapter-") as tmp_dir:
        tmp = Path(tmp_dir)
        preview_paths: list[Path] = []
        for index, preview_bytes in enumerate(preview_bytes_list, start=1):
            path = tmp / f"preview-{index}.pdf"
            path.write_bytes(preview_bytes)
            preview_paths.append(path)

        merged_path = tmp / "merged.pdf"
        header_path = tmp / "header.pdf"
        output_path = tmp / "chapter.pdf"

        merge_args = [
            "-merge",
            str(cover_path),
            *[str(path) for path in preview_paths],
            "-process-struct-trees",
            "-subformat",
            "PDF/UA-2",
            "-o",
            str(merged_path),
        ]
        _run_cpdf(cpdf_path, merge_args)

        _run_cpdf(
            cpdf_path,
            [
                "-add-text",
                textbook_header,
                "-font",
                "Helvetica",
                "-font-size",
                "12",
                "-process-struct-trees",
                "-topleft",
                "35 30",
                str(merged_path),
                "2-end",
                "-o",
                str(header_path),
            ],
        )
        _run_cpdf(
            cpdf_path,
            [
                "-add-text",
                chapter_title,
                "-font",
                "Helvetica",
                "-font-size",
                "12",
                "-process-struct-trees",
                "-topleft",
                "35 50",
                str(header_path),
                "2-end",
                "-o",
                str(output_path),
            ],
        )
        return output_path.read_bytes()
