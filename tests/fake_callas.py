"""Test-only stand-in for the callas pdfToolbox merger (no network, no real callas)."""
from pathlib import Path
from typing import Sequence

import pymupdf


class FakeCallasMerger:
    """Mimics ``make_callas_pdf_merger``: merges parts in order into ``output``."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, parts: Sequence[Path], output: Path, label: str) -> None:
        self.calls.append(([Path(p).name for p in parts], label))
        document = pymupdf.open()
        try:
            for part in parts:
                with pymupdf.open(part) as source:
                    document.insert_pdf(source)
            document.save(output)
        finally:
            document.close()


# --------------------------------------------------------------------------- process / separations
import shutil
import tempfile
from types import SimpleNamespace

_NS = 'xmlns="http://www.callassoftware.com/namespace/pi4"'


def _report(results: str, resources: str = "") -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8" ?><report {_NS}>'
        f"<document><resources>{resources}</resources></document>"
        f"<results>{results}</results></report>"
    )


def icc_report_xml(pages: Sequence[int]) -> str:
    hits = "".join(
        f'<hit type="Image" page="PAG{page}" llx="0" lly="0" urx="10" ury="10">'
        '<imagestate image_id="IMG1" v_ppi="300" h_ppi="300"/></hit>'
        for page in pages
    )
    return _report(f'<hits rule_id="RUL39" severity="Info">{hits}</hits>' if pages else "")


def fonts_report_xml(fonts: Sequence[tuple[str, str, bool]], *, hits_per_font: int = 3, invisible: Sequence[str] = ()) -> str:
    """``fonts`` — (имя, тип, встроен). Срабатываний на шрифт несколько: считать надо шрифты."""
    resources = "<fonts>" + "".join(
        f'<font id="FNT{i}" type="{kind}" embedded="{int(embedded)}" subset="0"><name>{name}</name></font>'
        for i, (name, kind, embedded) in enumerate(fonts, start=1)
    ) + "</fonts>"
    hits = ""
    for i, (name, kind, _embedded) in enumerate(fonts, start=1):
        hit_type = "TextInvisible" if name in invisible else "TextFill"
        for n in range(hits_per_font):
            hits += (
                f'<hit type="{hit_type}" page="PAG{1 + n % 2}" llx="0" lly="0" urx="9" ury="9">'
                f'<textstate font_id="FNT{i}"/><triggers><trigger condition_id="CND1">'
                f"<value>{kind}</value><detail>{name}</detail></trigger></triggers></hit>"
            )
    return _report(f'<hits rule_id="RUL1" severity="Warning">{hits}</hits>' if fonts else "", resources)


def ppi_report_xml(images: Sequence[tuple[int, float, int]]) -> str:
    """``images`` — (страница, ppi, bits_per_color_component)."""
    resources = "<images>" + "".join(
        f'<image id="IMG{i}" bits_per_color_component="{bits}" treated_as_mask="0"/>'
        for i, (_page, _ppi, bits) in enumerate(images, start=1)
    ) + "</images>"
    hits = "".join(
        f'<hit type="Image" page="PAG{page}" llx="{i}" lly="0" urx="10" ury="10">'
        f'<imagestate image_id="IMG{i}" v_ppi="{ppi}" h_ppi="{ppi}"/></hit>'
        for i, (page, ppi, _bits) in enumerate(images, start=1)
    )
    return _report(f'<hits rule_id="RUL1" severity="Info">{hits}</hits>' if images else "", resources)


class FakeCallasProcessClient:
    """Фейковый клиент callas: ``process`` по профилю + ``separations`` без сети.

    ``profiles`` — профиль -> обработчик ``(pdf, mode) -> dict`` с ключами ``summary``
    (по умолчанию нули), ``xml`` (текст отчёта), ``fixed`` (``Path`` для копии как результата
    fix), ``exit_code``. Обработчик может быть исключением (будет выброшено).
    """

    def __init__(self, root: Path, profiles: dict | None = None, separations_factory=None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.profiles = dict(profiles or {})
        self.separations_factory = separations_factory
        self.calls: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def delete(self, job_id: str) -> None:
        self.deleted.append(job_id)

    def process(self, pdf, profile, mode, *, job_id=None, report=("xml",), **_kwargs):
        self.calls.append((profile, mode))
        handler = self.profiles.get(profile)
        if handler is None:
            raise RuntimeError(f"нет обработчика профиля {profile}")
        if isinstance(handler, Exception):
            raise handler
        spec = handler(Path(pdf), mode) if callable(handler) else dict(handler)
        out = self.root / (job_id or "job") / "out"
        out.mkdir(parents=True, exist_ok=True)
        report_xml = None
        if spec.get("xml") is not None:
            report_xml = out / "report.xml"
            report_xml.write_text(spec["xml"], encoding="utf-8")
        output_pdf = None
        if mode == "fix":
            output_pdf = out / "fixed.pdf"
            source = spec.get("fixed") or pdf
            shutil.copyfile(source, output_pdf)
        summary = {"errors": 0, "warnings": 0, "infos": 0, "fixups_applied": 0, "fixups_failed": 0}
        summary.update(spec.get("summary") or {})
        return SimpleNamespace(
            job_id=job_id, mode=mode, status=spec.get("status", "ok"),
            exit_code=spec.get("exit_code", 0), summary=summary,
            output_pdf=output_pdf, report_xml=report_xml,
        )

    def separations(self, pdf, *, resolution=50, job_id, **_kwargs):
        if self.separations_factory is None:
            raise RuntimeError("separations не настроены")
        self.calls.append(("separations", "sep_process"))
        out = self.root / job_id / "out"
        out.mkdir(parents=True, exist_ok=True)
        return self.separations_factory(Path(pdf), out)


def make_separations(out: Path, pages: Sequence[dict], *, size: tuple[int, int] = (40, 40)) -> dict:
    """Сепарации PNG. ``pages`` — по странице dict ``k100`` (доля чистого K100) и ``rich`` (доля составного)."""
    from PIL import Image

    width, height = size
    total = width * height
    result: dict = {}
    for number, spec in enumerate(pages, start=1):
        pure = int(total * spec.get("k100", 0))
        rich = int(total * spec.get("rich", 0))
        channels = {name: bytearray([255] * total) for name in ("Cyan", "Magenta", "Yellow", "Black")}
        for index in range(pure):
            channels["Black"][index] = 0
        for index in range(pure, pure + rich):
            channels["Black"][index] = 0
            for name in ("Cyan", "Magenta", "Yellow"):
                channels[name][index] = 0
        for name, data in channels.items():
            path = out / f"page-{number:04d}_sep_{name}.png"
            Image.frombytes("L", size, bytes(data)).save(path)
            result[(number, name)] = path
    return result
