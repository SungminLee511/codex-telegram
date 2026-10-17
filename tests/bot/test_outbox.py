"""Tests for the drop box Codex leaves files in."""

from pathlib import Path

import pytest

from src.bot.utils import outbox


@pytest.fixture()
def root(tmp_path):
    box = tmp_path / "outbox"
    box.mkdir()
    return box


def put(root: Path, name: str, size: int = 10) -> Path:
    path = root / name
    path.write_bytes(b"x" * size)
    return path


class FakeMessage:
    """Records what the bot tried to send."""

    def __init__(self, fail=False):
        self.photos = []
        self.documents = []
        self.texts = []
        self.fail = fail

    async def reply_photo(self, photo, caption=None, filename=None):
        if self.fail:
            raise RuntimeError("telegram said no")
        self.photos.append((filename, caption))

    async def reply_document(self, document, filename=None):
        if self.fail:
            raise RuntimeError("telegram said no")
        self.documents.append(filename)

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """The real sweep paces itself; tests should not."""

    async def instant(_seconds):
        return None

    monkeypatch.setattr(outbox.asyncio, "sleep", instant)


# --- what counts as waiting --------------------------------------------------


def test_a_dropped_file_is_pending(root):
    put(root, "plot.png")

    assert [p.name for p in outbox.pending(root)] == ["plot.png"]


def test_an_empty_box_has_nothing_pending(root):
    assert outbox.pending(root) == []


def test_a_missing_box_is_not_an_error(tmp_path):
    assert outbox.pending(tmp_path / "nope") == []


def test_files_are_ordered_by_name(root):
    put(root, "2-after.png")
    put(root, "1-before.png")

    assert [p.name for p in outbox.pending(root)] == ["1-before.png", "2-after.png"]


def test_directories_are_ignored(root):
    (root / "subdir").mkdir()

    assert outbox.pending(root) == []


def test_hidden_files_are_ignored(root):
    put(root, ".gitkeep")

    assert outbox.pending(root) == []


def test_a_file_still_being_written_is_skipped(root):
    """A half-written render would arrive as a corrupt image."""
    put(root, "render.png.tmp")
    put(root, "download.part")

    assert outbox.pending(root) == []


def test_a_file_another_bot_is_sending_is_skipped(root):
    put(root, "plot.png.sending-4242")

    assert outbox.pending(root) == []


# --- photo or document -------------------------------------------------------


def test_an_image_goes_as_a_photo(root):
    assert outbox.is_photo(root / "a.png", size=100)


def test_case_does_not_matter(root):
    assert outbox.is_photo(root / "A.JPG", size=100)


def test_a_non_image_goes_as_a_document(root):
    assert not outbox.is_photo(root / "notes.pdf", size=100)


def test_a_huge_image_goes_as_a_document(root):
    """Telegram refuses a photo over 10 MB; as a document it fits."""
    assert not outbox.is_photo(root / "big.png", size=outbox.PHOTO_MAX_BYTES + 1)


# --- the sweep ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_picture_is_sent_and_removed(root):
    put(root, "plot.png")
    message = FakeMessage()

    result = await outbox.deliver(message, root)

    assert message.photos == [("plot.png", "plot.png")]
    assert result.sent == ["plot.png"]
    assert outbox.pending(root) == []


@pytest.mark.asyncio
async def test_the_real_name_is_sent_not_the_claim_marker(root):
    """The file is renamed while in flight; the user must not see that."""
    put(root, "plot.png")
    message = FakeMessage()

    await outbox.deliver(message, root)

    name, caption = message.photos[0]
    assert outbox._CLAIM_MARK not in (name or "")
    assert outbox._CLAIM_MARK not in (caption or "")


@pytest.mark.asyncio
async def test_a_plain_file_is_sent_as_a_document(root):
    put(root, "run.log")
    message = FakeMessage()

    await outbox.deliver(message, root)

    assert message.documents == ["run.log"]
    assert message.photos == []


@pytest.mark.asyncio
async def test_several_files_all_go(root):
    put(root, "1.png")
    put(root, "2.log")
    message = FakeMessage()

    result = await outbox.deliver(message, root)

    assert result.sent == ["1.png", "2.log"]
    assert outbox.pending(root) == []


@pytest.mark.asyncio
async def test_an_empty_box_sends_nothing(root):
    message = FakeMessage()

    result = await outbox.deliver(message, root)

    assert not result.anything_happened
    assert message.photos == [] and message.documents == []


@pytest.mark.asyncio
async def test_a_failed_send_keeps_the_file(root):
    """A dropped connection must not cost the user the file."""
    put(root, "plot.png")
    message = FakeMessage(fail=True)

    result = await outbox.deliver(message, root)

    assert result.failed == ["plot.png"]
    assert [p.name for p in outbox.pending(root)] == ["plot.png"]


@pytest.mark.asyncio
async def test_an_oversized_file_is_left_behind_and_named(root):
    put(root, "dump.bin", size=outbox.DOCUMENT_MAX_BYTES + 1)
    message = FakeMessage()

    result = await outbox.deliver(message, root)

    assert result.too_big == ["dump.bin"]
    assert [p.name for p in outbox.pending(root)] == ["dump.bin"]
    assert message.documents == []


@pytest.mark.asyncio
async def test_the_note_explains_only_the_problems(root):
    put(root, "fine.png")
    put(root, "dump.bin", size=outbox.DOCUMENT_MAX_BYTES + 1)
    message = FakeMessage()

    result = await outbox.deliver(message, root)

    note = result.note()
    assert "dump.bin" in note
    assert "fine.png" not in note


@pytest.mark.asyncio
async def test_a_clean_sweep_says_nothing(root):
    put(root, "fine.png")

    result = await outbox.deliver(FakeMessage(), root)

    assert result.note() is None


@pytest.mark.asyncio
async def test_only_so_many_files_go_per_message(root, monkeypatch):
    """A loop that writes a thousand frames must not become a flood-ban."""
    monkeypatch.setattr(outbox, "MAX_FILES_PER_SWEEP", 3)
    for i in range(5):
        put(root, f"{i}.log")
    message = FakeMessage()

    result = await outbox.deliver(message, root)

    assert len(result.sent) == 3
    assert result.held_back == 2
    assert len(outbox.pending(root)) == 2


@pytest.mark.asyncio
async def test_the_held_back_files_go_on_the_next_sweep(root, monkeypatch):
    monkeypatch.setattr(outbox, "MAX_FILES_PER_SWEEP", 2)
    for i in range(4):
        put(root, f"{i}.log")

    await outbox.deliver(FakeMessage(), root)
    second = await outbox.deliver(FakeMessage(), root)

    assert second.sent == ["2.log", "3.log"]
    assert outbox.pending(root) == []


@pytest.mark.asyncio
async def test_a_sweep_stops_at_its_byte_budget(root, monkeypatch):
    monkeypatch.setattr(outbox, "MAX_TOTAL_BYTES_PER_SWEEP", 100)
    put(root, "1.log", size=80)
    put(root, "2.log", size=80)
    message = FakeMessage()

    result = await outbox.deliver(message, root)

    assert result.sent == ["1.log"]
    assert result.held_back == 1


@pytest.mark.asyncio
async def test_held_back_files_are_mentioned(root, monkeypatch):
    monkeypatch.setattr(outbox, "MAX_FILES_PER_SWEEP", 1)
    put(root, "1.log")
    put(root, "2.log")

    result = await outbox.deliver(FakeMessage(), root)

    assert "still in outbox" in result.note()


@pytest.mark.asyncio
async def test_an_oversized_file_does_not_stop_the_smaller_ones(root):
    put(root, "1-huge.bin", size=outbox.DOCUMENT_MAX_BYTES + 1)
    put(root, "2-small.log")
    message = FakeMessage()

    result = await outbox.deliver(message, root)

    assert result.sent == ["2-small.log"]
    assert result.too_big == ["1-huge.bin"]


@pytest.mark.asyncio
async def test_a_file_claimed_by_another_bot_is_left_alone(root, monkeypatch):
    """Two bots share the directory; only one may send a given file."""
    put(root, "plot.png")
    monkeypatch.setattr(outbox, "_claim", lambda path: None)
    message = FakeMessage()

    result = await outbox.deliver(message, root)

    assert result.sent == []
    assert message.photos == []
    assert [p.name for p in outbox.pending(root)] == ["plot.png"]


@pytest.mark.asyncio
async def test_claiming_hides_the_file_from_a_second_sweep(root):
    path = put(root, "plot.png")

    claimed = outbox._claim(path)

    assert claimed is not None
    assert outbox.pending(root) == []


@pytest.mark.asyncio
async def test_a_vanished_file_does_not_break_the_sweep(root, monkeypatch):
    """Listing the directory and reading it are two separate moments."""
    path = put(root, "plot.png")
    real_pending = outbox.pending

    def pending_then_delete(r=root):
        found = real_pending(r)
        path.unlink()
        return found

    monkeypatch.setattr(outbox, "pending", pending_then_delete)

    result = await outbox.deliver(FakeMessage(), root)

    assert result.sent == []
