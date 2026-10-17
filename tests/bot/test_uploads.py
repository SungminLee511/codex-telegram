"""Tests for the Telegram upload inbox."""

import time

import pytest

from src.bot.utils import uploads


@pytest.fixture(autouse=True)
def inbox(tmp_path, monkeypatch):
    """Point the inbox at a temp dir so tests never touch the real one."""
    root = tmp_path / "uploads"
    monkeypatch.setattr(uploads, "UPLOAD_ROOT", root)
    return root


def test_saves_under_a_dated_folder(inbox):
    path = uploads.save_bytes(b"hello", "notes.txt", 42)

    assert path.read_bytes() == b"hello"
    assert path.parent.parent == inbox
    assert path.parent.name == time.strftime("%Y-%m-%d")
    assert path.name == "42_notes.txt"


def test_filename_is_sanitised_and_traversal_defeated(inbox):
    path = uploads.save_bytes(b"x", "../../etc/pa ss;wd", 7)

    assert path.name == "7_pa_ss_wd"
    assert inbox in path.parents


def test_missing_filename_gets_a_default(inbox):
    assert uploads.save_bytes(b"x", None, 1).name == "1_upload.bin"


def test_same_name_twice_does_not_overwrite(inbox):
    first = uploads.save_bytes(b"one", "data.csv", 5)
    second = uploads.save_bytes(b"two", "data.csv", 5)

    assert first != second
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"


def test_small_text_is_inlined_in_the_prompt(inbox):
    path = uploads.save_bytes(b"col_a,col_b\n1,2\n", "data.csv", 9)
    prompt = uploads.build_prompt(path, "check this")

    assert "check this" in prompt
    assert str(path) in prompt
    assert "col_a,col_b" in prompt


def test_large_file_is_referenced_by_path_only(inbox):
    big = b"x" * (uploads.INLINE_TEXT_LIMIT + 1)
    path = uploads.save_bytes(big, "big.txt", 9)
    prompt = uploads.build_prompt(path, None)

    assert str(path) in prompt
    assert "Read it from that path" in prompt
    assert "xxxx" not in prompt


def test_binary_file_is_not_inlined_and_does_not_raise(inbox):
    path = uploads.save_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe", "img.png", 3)
    prompt = uploads.build_prompt(path, "what is this", kind="image")

    assert uploads.read_inline_text(path) is None
    assert "[uploaded image]" in prompt
    assert str(path) in prompt


def test_caption_absent_falls_back_to_generic_line(inbox):
    path = uploads.save_bytes(b"data", "f.bin", 1)
    assert uploads.build_prompt(path, "   ").startswith("I uploaded a file.")


def test_cleanup_removes_only_old_day_folders(inbox):
    import os

    fresh = uploads.save_bytes(b"new", "new.txt", 1)
    old_dir = inbox / "2000-01-01"
    old_dir.mkdir(parents=True)
    (old_dir / "ancient.txt").write_text("old")
    stale = time.time() - 365 * 86400
    os.utime(old_dir, (stale, stale))

    removed = uploads.cleanup(retention_days=30)

    assert removed == 1
    assert not old_dir.exists()
    assert fresh.exists()


def test_cleanup_on_missing_root_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_ROOT", tmp_path / "nope")
    assert uploads.cleanup() == 0


def test_human_size_units():
    assert uploads.human_size(512) == "512 B"
    assert uploads.human_size(2048) == "2 KB"
    assert uploads.human_size(5 * 1024 * 1024) == "5.0 MB"


async def test_save_telegram_file_writes_through(inbox):
    class FakeFile:
        async def download_to_drive(self, path):
            with open(path, "wb") as handle:
                handle.write(b"downloaded")

    path = await uploads.save_telegram_file(FakeFile(), "report.pdf", 11)
    assert path.read_bytes() == b"downloaded"
    assert path.name == "11_report.pdf"
