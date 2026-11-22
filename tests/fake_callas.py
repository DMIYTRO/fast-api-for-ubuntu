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


#: Цветовые пространства отчёта overprint: id -> (type, channels, имена красок / ICC-профиль).
OVERPRINT_SPACES = {
    "CS1": ("DeviceCMYK", 4, ()),
    "CS2": ("DeviceRGB", 3, ()),
    "CS3": ("Separation", 1, ("White",)),
    "CS4": ("DeviceN", 2, ("White", "Cyan")),
    "CS5": ("ICCBased", 4, ("ISO Coated v2 (ECI)",)),
    "CS6": ("Separation", 1, ("Black",)),
    "CS7": ("Separation", 1, ("Varnish",)),
    "CS8": ("Separation", 1, ("PANTONE 485 C",)),
    "CS9": ("DeviceGray", 1, ()),
}


def overprint_report_xml(hits: Sequence[tuple], spaces: dict | None = None) -> str:
    """Отчёт «Overprint - any object». ``hits`` — (type, page, colorspace id | None, values | None
    [, элемент состояния: "gstate" | "textstate" | None]). Формат — как у реального отчёта
    pdfToolbox 17 (docs/callas-profiles.md, «Overprint - any object»)."""
    resources = "<colorspaces>"
    for space_id, (kind, channels, names) in (spaces or OVERPRINT_SPACES).items():
        inner = ""
        if kind == "DeviceN":
            inner = "<alternate_colorants>" + "".join(
                f"<colorant><name>{name}</name><alternate_values><li>0.0</li></alternate_values></colorant>"
                for name in names
            ) + "</alternate_colorants>"
        elif names:
            inner = f"<name>{names[0]}</name><alternate_values><li>1.0</li></alternate_values>"
        resources += f'<colorspace id="{space_id}" type="{kind}" channels="{channels}">{inner}</colorspace>'
    resources += "</colorspaces>"
    body = ""
    for hit in hits:
        kind, page, space, values = hit[:4]
        state = hit[4] if len(hit) > 4 else ("textstate" if kind.startswith("Text") else "gstate")
        side = "stroke" if "Stroke" in kind and "Fill" not in kind else "fill"
        attrs = f'overprint_mode="1" overprint_for_{side}="1"'
        if space:
            attrs += f' {side}_colorspace="{space}"'
        if values is not None:
            attrs += f' {side}_colorant_values="{"/".join(str(value) for value in values)}"'
        element = f"<{state} {attrs}></{state}>" if state else ""
        body += (
            f'<hit type="{kind}" page="PAG{page}" llx="0" lly="0" urx="10" ury="10">{element}'
            '<triggers><trigger condition_id="CND2"><value>is true</value><detail></detail></trigger></triggers></hit>'
        )
    return _report(f'<hits rule_id="RUL1" severity="Error">{body}</hits>' if hits else "", resources)


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


def make_separations(out: Path, pages: Sequence[dict], *, size: tuple[int, int] = (200, 200)) -> dict:
    """Сепарации PNG (50 ppi: 200 px ≈ 100 мм). ``pages`` — по странице dict долей площади:

    ``k100`` — сплошное поле чистого K100 (квадрат от левого верхнего угла), ``rich`` — поле
    составного чёрного (квадрат от правого нижнего угла), ``text`` — тонкие штрихи чистого K100
    (горизонтальные линии 2 px ≈ 1 мм через 3 px от середины страницы), как текст или линии.
    """
    import math

    from PIL import Image

    width, height = size
    total = width * height

    def square(count: int, *, from_end: bool) -> list[int]:
        side = max(1, math.ceil(math.sqrt(count)))
        cells = [row * width + col for row in range(side) for col in range(side)][:count]
        return [total - 1 - index for index in cells] if from_end else cells

    def strokes(count: int) -> list[int]:
        cells: list[int] = []
        row = height // 2
        while len(cells) < count and row < height:
            if (row - height // 2) % 5 < 2:
                cells.extend(row * width + col for col in range(width))
            row += 1
        return cells[:count]

    result: dict = {}
    for number, spec in enumerate(pages, start=1):
        channels = {name: bytearray([255] * total) for name in ("Cyan", "Magenta", "Yellow", "Black")}
        pure = square(int(total * spec.get("k100", 0)), from_end=False)
        pure += strokes(int(total * spec.get("text", 0)))
        for index in pure:
            channels["Black"][index] = 0
        for index in square(int(total * spec.get("rich", 0)), from_end=True):
            for name in ("Cyan", "Magenta", "Yellow", "Black"):
                channels[name][index] = 0
        for name, data in channels.items():
            path = out / f"page-{number:04d}_sep_{name}.png"
            Image.frombytes("L", size, bytes(data)).save(path)
            result[(number, name)] = path
    return result


def make_tac_separations(out: Path, pages: Sequence[dict], *, size: tuple[int, int] = (40, 40)) -> dict:
    """Сепарации PNG для замера TAC. ``pages`` — по странице dict ``cmyk`` (проценты C, M, Y, K
    закрашенного участка) и ``share`` (доля площади участка, по умолчанию вся страница); остальное белое."""
    from PIL import Image

    width, height = size
    total = width * height
    result: dict = {}
    for number, spec in enumerate(pages, start=1):
        painted = int(total * spec.get("share", 1.0))
        for ink, percent in zip(("Cyan", "Magenta", "Yellow", "Black"), spec.get("cmyk", (0, 0, 0, 0))):
            level = round(255 - percent / 100 * 255)
            data = bytes([level] * painted + [255] * (total - painted))
            path = out / f"page-{number:04d}_sep_{ink}.png"
            Image.frombytes("L", size, data).save(path)
            result[(number, ink)] = path
    return result
