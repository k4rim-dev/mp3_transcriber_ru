import os
import shutil
import tarfile
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.request import urlopen


FORMATS = (".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".mp4")
MODES = {"Быстрее": ("base", 1), "Точнее": ("small", 5)}
MODEL_DIR = Path(__file__).resolve().parent / "models"
WHISPER_FILES = ("config.json", "tokenizer.json", "vocabulary.txt", "model.bin")
WHISPER_MB = {"base": 148, "small": 486}
SAMPLE_RATE = 16_000
RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION = "sherpa-onnx-pyannote-segmentation-3-0"
EMBEDDING = "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"


@dataclass(frozen=True)
class Options:
    mode: str
    timestamps: bool
    speakers: bool


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str
    speaker: int | None = None


def speaker_cues(segments, turns):
    turns = iter(turns)
    upcoming = next(turns, None)
    active = []
    names = {}

    for segment in segments:
        words = segment.words or [Cue(segment.start, segment.end, segment.text)]
        cue = None
        for word in words:
            active = [turn for turn in active if turn.end > word.start]
            while upcoming is not None and upcoming.start < word.end:
                if upcoming.end > word.start:
                    active.append(upcoming)
                upcoming = next(turns, None)

            overlaps = defaultdict(float)
            for turn in active:
                length = min(turn.end, word.end) - max(turn.start, word.start)
                if length > 0:
                    overlaps[turn.speaker] += length
            number = None
            if overlaps:
                speaker = max(overlaps, key=overlaps.__getitem__)
                number = names.setdefault(speaker, len(names) + 1)

            text = word.word if segment.words else word.text
            if cue is not None and cue.speaker == number:
                cue = Cue(cue.start, max(cue.end, word.end), cue.text + text, number)
            else:
                if cue is not None:
                    yield cue
                cue = Cue(word.start, word.end, text, number)
        if cue is not None:
            yield cue


def timestamp(seconds):
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def render(cues, options):
    paragraphs = []
    previous = None
    paragraph_length = 0
    for cue in cues:
        content = cue.text.strip()
        if not content:
            continue
        join_paragraph = (
            not options.timestamps
            and previous is not None
            and cue.speaker == previous.speaker
            and cue.start - previous.end < 2
            and (paragraph_length < 400 or not previous.text.rstrip().endswith((".", "!", "?", "…")))
        )
        previous = cue
        if join_paragraph:
            paragraphs[-1].append(content)
            paragraph_length += len(content) + 1
            continue
        if options.speakers:
            label = f"Спикер {cue.speaker}" if cue.speaker else "Неизвестный спикер"
            content = f"{label}: {content}"
        if options.timestamps:
            content = f"[{timestamp(cue.start)}] {content}"
        paragraphs.append([content])
        paragraph_length = len(content)
    separator = "\n" if options.timestamps else "\n\n"
    if not paragraphs:
        return "Речь не обнаружена.\n"
    return separator.join(" ".join(parts) for parts in paragraphs) + "\n"


def save_text(source, text):
    # оставляю готовый txt рядом с исходником и сохраняю предыдущие версии
    ready = None
    try:
        with NamedTemporaryFile(
            dir=source.parent, prefix=".transcriber-", suffix=".tmp",
            mode="w", encoding="utf-8", delete=False,
        ) as file:
            ready = Path(file.name)
            file.write(text)
        index = 1
        while True:
            name = source.stem if index == 1 else f"{source.stem} ({index})"
            output = source.with_name(name + ".txt")
            try:
                if os.name == "nt":
                    ready.rename(output)
                else:
                    os.link(ready, output)
            except FileExistsError:
                index += 1
            else:
                return output
    finally:
        if ready is not None:
            ready.unlink(missing_ok=True)


def fetch_model(path, url, member=None, progress=None, scratch=None):
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=scratch or path.parent) as tmp:
        download = Path(tmp) / "download"
        with urlopen(url, timeout=60) as response, download.open("wb") as file:
            if response.status != 200:
                raise OSError(f"Неожиданный ответ сервера при загрузке {path.name}.")
            total = int(response.headers.get("Content-Length", 0))
            received = 0
            while chunk := response.read(1024 * 1024):
                file.write(chunk)
                received += len(chunk)
                if progress is not None and total:
                    progress(100 * received / total)
        if not received or (total and received != total):
            raise OSError(f"Не удалось полностью скачать {path.name}.")
        if member:
            extracted = Path(tmp) / path.name
            with tarfile.open(download, "r:bz2") as archive:
                entry = archive.getmember(member)
                if not entry.isfile():
                    raise OSError(f"В архиве нет модели {path.name}.")
                with archive.extractfile(entry) as model, extracted.open("wb") as file:
                    shutil.copyfileobj(model, file)
            download = extracted
        download.replace(path)


class DownloadDeclined(Exception):
    pass


class Transcriber:
    def __init__(self, confirm_download=None):
        self.whisper = None
        self.whisper_name = None
        self.diarizer = None
        self.threads = min(4, os.cpu_count() or 1)
        self.confirm_download = confirm_download

    def load_whisper(self, name, progress):
        if self.whisper is not None and self.whisper_name == name:
            return self.whisper

        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
        from huggingface_hub.errors import LocalEntryNotFoundError

        self.whisper = None
        progress(f"Подготовка Whisper {name}", None)
        path = MODEL_DIR / "whisper" / name
        if not all((path / filename).is_file() for filename in WHISPER_FILES):
            try:
                cached = Path(download_model(name, local_files_only=True, use_auth_token=False))
            except LocalEntryNotFoundError:
                cached = None
            if cached is not None and all((cached / filename).is_file() for filename in WHISPER_FILES):
                path = cached
            else:
                scratch = None
                if self.confirm_download:
                    scratch = self.confirm_download([(f"Whisper {name}", WHISPER_MB[name])], path)
                stage = f"Скачиваю Whisper {name}"
                progress(stage, None)
                repo = f"https://huggingface.co/Systran/faster-whisper-{name}/resolve/main"
                for filename in WHISPER_FILES:
                    fetch_model(
                        path / filename, f"{repo}/{filename}?download=true",
                        progress=(lambda percent: progress(stage, percent)) if filename == "model.bin" else None,
                        scratch=scratch,
                    )
        self.whisper = WhisperModel(
            str(path), device="cpu", compute_type="int8", cpu_threads=self.threads,
        )
        self.whisper_name = name
        return self.whisper

    def diarize(self, audio, progress):
        if self.diarizer is None:
            import sherpa_onnx as sherpa

            progress("Подготовка моделей спикеров", None)
            segmentation = MODEL_DIR / "speakers" / "segmentation.onnx"
            embedding = MODEL_DIR / "speakers" / EMBEDDING
            missing = [
                (label, size) for path, label, size in (
                    (segmentation, "Pyannote", 6), (embedding, "3D-Speaker", 40),
                ) if not path.is_file()
            ]
            scratch = self.confirm_download(missing, segmentation.parent) if missing and self.confirm_download else None
            fetch_model(
                segmentation,
                f"{RELEASES}/speaker-segmentation-models/{SEGMENTATION}.tar.bz2",
                f"{SEGMENTATION}/model.onnx",
                scratch=scratch,
            )
            fetch_model(
                embedding, f"{RELEASES}/speaker-recongition-models/{EMBEDDING}",
                scratch=scratch,
            )
            config = sherpa.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa.OfflineSpeakerSegmentationPyannoteModelConfig(str(segmentation)),
                    num_threads=self.threads,
                ),
                embedding=sherpa.SpeakerEmbeddingExtractorConfig(
                    model=str(embedding), num_threads=self.threads,
                ),
                clustering=sherpa.FastClusteringConfig(num_clusters=-1, threshold=0.5),
            )
            if not config.validate():
                raise RuntimeError("Не удалось загрузить модели спикеров.")
            self.diarizer = sherpa.OfflineSpeakerDiarization(config)

        progress("Размечаю спикеров", None)

        def update(processed, total):
            if total:
                progress("Размечаю спикеров", 100 * processed / total)
            return 0

        return self.diarizer.process(audio, callback=update).sort_by_start_time()

    def transcribe(self, source, options, progress):
        return save_text(source, self.transcribe_text(source, options, progress))

    def transcribe_text(self, source, options, progress):
        from faster_whisper.audio import decode_audio
        from av.error import FFmpegError

        progress("Читаю аудио", None)
        try:
            audio = decode_audio(str(source), sampling_rate=SAMPLE_RATE)
        except (FFmpegError, IndexError, ValueError) as exc:
            raise ValueError(f"Не удалось прочитать аудиодорожку: {exc}") from exc
        if not audio.size:
            raise ValueError("Аудиодорожка пуста.")

        name, beam_size = MODES[options.mode]
        model = self.load_whisper(name, progress)
        progress("Распознаю речь", 0)
        segments, info = model.transcribe(
            audio, language="ru", beam_size=beam_size, vad_filter=True,
            word_timestamps=options.speakers,
        )
        recognized = []
        for segment in segments:
            if segment.text.strip():
                recognized.append(segment)
            progress("Распознаю речь", min(100, 100 * segment.end / info.duration))

        if options.speakers and recognized:
            cues = speaker_cues(recognized, self.diarize(audio, progress))
        else:
            cues = (Cue(s.start, s.end, s.text) for s in recognized)
        return render(cues, options)


def worker(jobs, events):
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    def confirm_download(models, path):
        events.put(("download", (models, path)))
        scratch = jobs.get()
        if scratch is None:
            raise DownloadDeclined
        return scratch

    transcriber = Transcriber(confirm_download)
    while True:
        source, options = jobs.get()
        last_stage = None
        last_update = 0

        def progress(stage, percent):
            nonlocal last_stage, last_update
            now = time.monotonic()
            if stage != last_stage or now - last_update >= 0.2:
                events.put(("progress", (stage, percent)))
                last_stage = stage
                last_update = now

        try:
            output = transcriber.transcribe_text(source, options, progress)
        except DownloadDeclined:
            events.put(("cancelled", None))
        except Exception as exc:
            events.put(("error", str(exc) or type(exc).__name__))
        else:
            events.put(("text", (source, output)))
