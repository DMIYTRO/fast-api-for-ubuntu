"""Собирает профиль callas «Image Magic/Apply QuickFix config.kfpx» без GUI pdfToolbox.

Профиль из одного шага: QuickFix на JavaScript возвращает JSON из переменной
``quickfix_config`` (передаётся в задание ``process`` через ``variables``). Вся логика
выбора исправления остаётся в Python (``processing/pdf_resize_policy.py``).

    .venv/bin/python scripts/build_quickfix_profile.py "callas_profiles/Apply QuickFix config.kfpx"
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

VARIABLE = "quickfix_config"


def _hex(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex


def build() -> str:
    now = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%S+00'00'").replace("'", "&apos;")
    qf_id = _hex("Q")
    mp_id5 = _hex("M")
    script = f"JSON.parse(app.vars.{VARIABLE});&#10;"
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<pdfpreflight version="23.0">
	<package>
		<id3>{_hex()}</id3>
		<exportfingerprint>{mp_id5}</exportfingerprint>
		<pdfxoutintentlist>
		</pdfxoutintentlist>
		<iccprofilelist>
		</iccprofilelist>
		<ccpolicies>
		</ccpolicies>
		<occdsetups>
		</occdsetups>
		<curves>
		</curves>
		<fspolicies>
		</fspolicies>
		<resources>
		</resources>
		<variables>
		</variables>
		<conditions>
		</conditions>
		<rules>
		</rules>
		<rulesets>
		</rulesets>
		<fixups>
		</fixups>
		<fixupsets>
		</fixupsets>
		<actions>
		</actions>
		<quickfixes>
			<quickfix>
				<id1>{qf_id}</id1>
				<id2>{_hex("Q")}</id2>
				<id3>{_hex()}</id3>
				<id4>{_hex()}</id4>
				<name>QF_Apply_Config</name>
				<comment>QuickFix JSON из переменной {VARIABLE}</comment>
				<flags edit="1" visible="1"></flags>
				<metalist>
					<metadata>
						<metakey>custom_id</metakey>
						<metavalue></metavalue>
					</metadata>
				</metalist>
				<creation>{now}</creation>
				<modification>{now}</modification>
				<qfcfgs>
					<qfcfg>
						<qffeat>javascript</qffeat>
						<qfinstructions>
							<qfinstruction>
								<qfparams>
									<qfparam key="javascript">{script}</qfparam>
								</qfparams>
							</qfinstruction>
						</qfinstructions>
					</qfcfg>
				</qfcfgs>
			</quickfix>
		</quickfixes>
		<profiles>
		</profiles>
		<metaprofs>
			<metaprof>
				<id1>{_hex("M")}</id1>
				<id2>{_hex("M")}</id2>
				<id3>{_hex()}</id3>
				<id4>{_hex()}</id4>
				<id5>{mp_id5}</id5>
				<name>Apply QuickFix config</name>
				<comment>Image Magic: применяет QuickFix JSON из переменной {VARIABLE}</comment>
				<flags edit="1" visible="1"></flags>
				<metalist>
				</metalist>
				<creation>{now}</creation>
				<modification>{now}</modification>
				<sequence>
					<step type="11">
						<enabled disablednext="-2">1</enabled>
						<customid></customid>
						<quickfix>{qf_id}</quickfix>
						<ppoptions>
							<ppoption ret="0" next="-1">
							</ppoption>
							<ppoption ret="1" next="-2">
							</ppoption>
						</ppoptions>
					</step>
				</sequence>
				<variables>
				</variables>
			</metaprof>
		</metaprofs>
		<profgroups>
		</profgroups>
	</package>
</pdfpreflight>
"""


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "Apply QuickFix config.kfpx")
    target.write_text(build().replace("\n", "\r\n"), encoding="utf-8")
    print(target)
