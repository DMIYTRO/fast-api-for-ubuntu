"""
Базовый класс валидатора файлов по профилю допечатной подготовки.
"""

from core.inspector import ImageMetadata
from config.profiles import PrePressProfile, DEFAULT_PROFILE
from .rules import ValidationResult, ValidationItem
from processing.profile_rules import evaluate_metadata_rules

class BaseValidator:
    """Базовый валидатор общих параметров файла."""

    def __init__(self, profile: PrePressProfile = None):
        self.profile = profile or DEFAULT_PROFILE

    def validate(self, meta: ImageMetadata) -> ValidationResult:
        """Проводит стандартные проверки файла."""
        items = []
        shared = evaluate_metadata_rules(
            profile=self.profile,
            size_mb=meta.size_mb,
            colorspace=meta.colorspace,
            dpi_x=meta.dpi_x,
            dpi_y=meta.dpi_y,
        )

        # 1. Проверка разрешения (DPI)
        dpi_ok = not any("разрешение " in value for value in shared.errors)
        items.append(ValidationItem(
            name="Разрешающая способность, DPI",
            actual_value=f"{int(meta.dpi)} DPI",
            target_value=str(int(self.profile.target_dpi)),
            passed=dpi_ok,
            message="Разрешение соответствует норме" if dpi_ok else f"Разрешение ниже требуемых {int(self.profile.target_dpi)} DPI"
        ))

        # 2. Проверка цветовой модели (CMYK)
        colorspace_ok = not shared.warnings
        items.append(ValidationItem(
            name="Цветовая модель",
            actual_value=meta.colorspace,
            target_value="CMYK",
            passed=True,
            message="Цветовая модель CMYK" if colorspace_ok else shared.warnings[0]
        ))

        # 3. Проверка цветового профиля (ICC)
        icc_ok = meta.icc_profile != "Не внедрен"
        items.append(ValidationItem(
            name="Цветовой профиль (ICC)",
            actual_value=meta.icc_profile,
            target_value="FOGRA39 / ISO Coated",
            passed=icc_ok,
            message="ICC профиль внедрен" if icc_ok else "Внимание: ICC профиль не найден в файле"
        ))

        # 4. Проверка объема файла (МБ)
        size_ok = not any("размер файла " in value for value in shared.errors)
        items.append(ValidationItem(
            name="Размер файла, Mb",
            actual_value=f"{meta.size_mb}",
            target_value=f"до {int(self.profile.max_file_size_mb)}",
            passed=size_ok,
            message="норма" if size_ok else f"Превышен лимит размера файла ({meta.size_mb} МБ)"
        ))

        overall_passed = all(item.passed for item in items)

        return ValidationResult(
            file_name=meta.file_name,
            format=meta.format,
            overall_passed=overall_passed,
            items=items
        )
