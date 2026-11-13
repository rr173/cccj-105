"""Build the eligibility sample corpus: workdir, clause files and JSONL inputs.

Everything here is synthetic.  The sample workdir is archived exactly like a
real one (identity-schema pipeline database + content-addressed raw blobs); the
clause files are authored **by hand** (this job never derives conditions from
natural language), and the application files are plain JSONL.

Clauses are created from the archived bodies' verified spans so the sample
files always carry accurate citations.  The negative clauses used by
``demo.py`` and the tests are also produced here as separate files:

* ``citations/E-9001-v1-shifted.json`` — one citation span shifted by 1
  character (the quote occurs elsewhere, so this proves the slice is checked
  literally);
* ``citations/E-9001-to-public.json`` — a clause bound to the public variant
  citing the team sentence that only exists in the full edition;
* ``citations/E-9001-v2-as-v1.json`` — the v1 clause text bound to version 2.
"""

from __future__ import annotations

import json
from pathlib import Path

from .eligsample import build_sample_workdir, span_of, version_for

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "scenario" / "eligibility"
CLAUSE_DIR = SCENARIO_DIR / "clauses"
INPUT_DIR = SCENARIO_DIR / "applications"

FULL_V1_QUOTES = {
    "PREAMBLE": "各申报单位：现就2026年度重点资助项目申报资格预审条件通告如下。",
    "ENTITY_KIND": "申报主体应为本市依法登记的高校、科研院所或企业",
    "ENTITY_YEARS": "登记年限不少于3年",
    "LEGAL_DATE": "法人登记日期须在2021年1月1日之后（含当日）",
    "PI": "项目负责人须具有博士学位，且从事研究工作不少于5年",
    "TEAM": "团队中至少2名成员应具有博士学位，且每人从事研究工作不少于3年",
    "INTEGRITY": "申报主体及团队成员近5年内不得有科研诚信失信记录",
    "CORRECTION": "证明材料尚未交齐的，列为待补证事项，不得直接认定为不符合资格",
}


SCOPE = {
    "subject": {
        "description": "申请主体（一个申报单位）",
        "fields": {
            "entity_type": "string",
            "registered_years": "decimal",
            "legal_registered_on": "date",
            "has_integrity_issue": "boolean",
        },
    },
    "member": {
        "description": "团队成员（每名成员一份资料；role=pi 的成员即项目负责人）",
        "fields": {
            "role": "string",
            "degree_level": "string",
            "years_experience": "decimal",
        },
    },
}


def citation(body: str, key: str) -> dict:
    return span_of(body, FULL_V1_QUOTES[key])


def v1_conditions(body: str) -> list[dict]:
    """The hand-authored condition set for E-9001 full edition v1.

    Each named condition carries a unique id, one verified citation, and a
    typed expression.  ``ROOT`` uses forward references (``PI_CONDITION`` and
    ``COUNT_DOCTORS`` are defined later) to exercise allowed forward
    resolution.
    """
    return [
        {
            "id": "ROOT",
            "scope": "subject",
            "description": "预审总条件：主体、负责人、团队、诚信四项均满足",
            "citation": citation(body, "PREAMBLE"),
            "expr": {"all": [
                {"ref": "ENTITY_KIND"},
                {"ref": "ENTITY_YEARS"},
                {"ref": "LEGAL_DATE"},
                {"ref": "PI_CONDITION"},
                {"ref": "COUNT_DOCTORS"},
                {"ref": "INTEGRITY_OK"},
            ]},
        },
        {
            "id": "ENTITY_KIND",
            "scope": "subject",
            "description": "申报主体须为高校、科研院所或企业",
            "citation": citation(body, "ENTITY_KIND"),
            "expr": {"field": "entity_type", "op": "in",
                     "values": ["university", "research_institute", "enterprise"]},
        },
        {
            "id": "ENTITY_YEARS",
            "scope": "subject",
            "description": "登记年限不少于 3 年（含边界）",
            "citation": citation(body, "ENTITY_YEARS"),
            "expr": {"field": "registered_years", "op": "ge", "value": "3"},
        },
        {
            "id": "LEGAL_DATE",
            "scope": "subject",
            "description": "法人登记日期在 2021-01-01 之后（含当日）",
            "citation": citation(body, "LEGAL_DATE"),
            "expr": {"field": "legal_registered_on", "op": "ge",
                     "value": "2021-01-01"},
        },
        {
            "id": "PI_DEGREE",
            "scope": "member",
            "description": "项目负责人具有博士学位",
            "citation": citation(body, "PI"),
            "expr": {"field": "degree_level", "op": "eq", "value": "doctorate"},
        },
        {
            "id": "PI_YEARS",
            "scope": "member",
            "description": "项目负责人从事研究工作不少于 5 年",
            "citation": citation(body, "PI"),
            "expr": {"field": "years_experience", "op": "ge", "value": "5"},
        },
        {
            "id": "PI_CONDITION",
            "scope": "subject",
            "description": "负责人条件：定位 role=pi 的成员，并在同一个人身上"
                           "同时核验学历与年限",
            "citation": citation(body, "PI"),
            "expr": {"count": {
                "predicate": {"all": [
                    {"field": "role", "op": "eq", "value": "pi"},
                    {"ref": "PI_DEGREE"},
                    {"ref": "PI_YEARS"},
                ]},
                "at_least": 1,
            }},
        },
        {
            "id": "COUNT_DOCTORS",
            "scope": "subject",
            "description": "核心团队至少 2 名博士，且每名博士成员从事研究不少于 3 年",
            "citation": citation(body, "TEAM"),
            "expr": {"count": {
                "predicate": {"all": [
                    {"field": "degree_level", "op": "eq", "value": "doctorate"},
                    {"field": "years_experience", "op": "ge", "value": "3"},
                ]},
                "at_least": 2,
            }},
        },
        {
            "id": "INTEGRITY_OK",
            "scope": "subject",
            "description": "近 5 年无科研诚信失信记录（本条款以“有失信记录”为反例；"
                           "无记录不等于已核实）",
            "citation": citation(body, "INTEGRITY"),
            "expr": {"field": "has_integrity_issue", "op": "eq", "value": False},
        },
    ]


def v1_clause(built: dict) -> dict:
    version = version_for(built, "E-9001", "E-9001-full-v1.html")
    return {
        "format": "eligibility-clause/1",
        "source_id": version["source_id"],
        "archive_id": version["archive_id"],
        "version_no": 1,
        "notice_key": "E-9001",
        "title": version["title"],
        "description": "2026 年度重点资助项目申报资格预审（完整版 v1）",
        "scope": SCOPE,
        "conditions": v1_conditions(version["body"]),
        "root": "ROOT",
    }


def v1_clause_v2_binding(built: dict) -> dict:
    """Same clause text, but explicitly pointed at version 2."""
    payload = v1_clause(built)
    payload["version_no"] = 2
    return payload


def public_clause(built: dict) -> dict:
    """Clause bound to the *public* variant, still quoting the team sentence."""
    public = version_for(built, "E-9001", "E-9001-public.html")
    full = version_for(built, "E-9001", "E-9001-full-v1.html")
    return {
        "format": "eligibility-clause/1",
        "source_id": public["source_id"],
        "archive_id": public["archive_id"],
        "version_no": 1,
        "notice_key": "E-9001",
        "title": public["title"],
        "description": "错误串档示例：绑定公众版却引用完整版团队条件",
        "scope": SCOPE,
        "conditions": v1_conditions(full["body"]),
        "root": "ROOT",
    }


def shifted_clause(built: dict) -> dict:
    payload = v1_clause(built)
    team = next(c for c in payload["conditions"] if c["id"] == "COUNT_DOCTORS")
    team["citation"] = {
        "start": team["citation"]["start"] + 1,
        "end": team["citation"]["end"] + 1,
        "quote": team["citation"]["quote"],
    }
    payload["description"] = "错误引文示例：COUNT_DOCTORS 的字符区间整体偏移 1"
    return payload


# ---------------------------------------------------------------------------
# Applications (all values synthetic)
# ---------------------------------------------------------------------------

def subject(**overrides) -> dict:
    values = {
        "entity_type": "research_institute",
        "registered_years": "3",
        "legal_registered_on": "2021-01-01",
        "has_integrity_issue": False,
    }
    values.update(overrides)
    return values


def pi(**overrides) -> dict:
    member = {"role": "pi", "degree_level": "doctorate",
              "years_experience": "5"}
    member.update(overrides)
    return member


def member(**overrides) -> dict:
    values = {"role": "co_investigator", "degree_level": "doctorate",
              "years_experience": "3"}
    values.update(overrides)
    return values


def app(application_id: str, *, subject_obj=None, items=None, complete=True,
        **extra) -> dict:
    record = {
        "application_id": application_id,
        "subject": subject() if subject_obj is None else subject_obj,
        "items": [] if items is None else items,
        "complete": complete,
    }
    record.update(extra)
    return record


def sample_applications() -> list[dict]:
    no_legal_date = subject()
    del no_legal_date["legal_registered_on"]
    return [
        # A1001 真假混合带未知：主体、负责人都已明确，团队计数区间 [2,3]
        #   仍达到“至少 2 名”的门槛；甲成员学历不合格是明确假分支。
        #   法人登记日期缺失，但已有反例，因此整体仍为“不满足”。
        app("APP-1001", items=[
            pi(),
            member(),
            member(degree_level="master", years_experience="8"),
            member(years_experience=None),
        ]),
        # A1002 完整空名单：complete=true + items=[] 是“确定没有成员”，
        #   不是“名单还没交”。
        app("APP-1002", items=[]),
        # A1003 名单未列全，但已知满足人数已够。
        app("APP-1003", items=[pi(), member(), member()], complete=False),
        # A1004 未知成员刚好卡住门槛：完整名单，已知 1 人、未知 1 人、假 1 人。
        app("APP-1004", items=[
            pi(),
            member(degree_level="master"),
            member(years_experience=None),
        ]),
        # A1005 两人各满足一半条件（学历/年限分属不同人），没有一人完整满足。
        app("APP-1005", items=[
            member(degree_level="doctorate", years_experience="1"),
            member(degree_level="master", years_experience="8"),
        ]),
        # A1006 精确十进制边界：3.0≥3 且 5.00≥5，均为含边界成立。
        app("APP-1006", subject_obj=subject(registered_years="3.0"),
            items=[pi(years_experience="5.00"),
                   member(years_experience="3.0")]),
        # A1007 诚信明确反例；法人登记日期缺失也不得把“不满足”改成“待补证”。
        app("APP-1007", subject_obj=dict(no_legal_date,
                                         has_integrity_issue=True),
            items=[pi(), member()]),
        # A1008 十进制写成 JSON 数字（类型不符）＋学历为 null。
        app("APP-1008", subject_obj=subject(registered_years=3),
            items=[pi(degree_level=None), member()]),
        # A1009 主体类型不在枚举内 → 明确不满足。
        app("APP-1009", subject_obj=subject(entity_type="individual"),
            items=[pi(), member()]),
        # A1010 主体与成员都是空对象：全部字段缺失 → 大量未知。
        app("APP-1010", subject_obj={}, items=[{}]),
        # A1011 一个成员的年限缺失，但其余材料已足以确定整体满足。
        app("APP-1011", items=[pi(), member(), member(years_experience=None)]),
        # A1012 名单未列全且有未知成员：人数上界不封顶。
        app("APP-1012", items=[pi(), member(degree_level="master"),
                               member(degree_level=None,
                                      years_experience=None)],
            complete=False),
    ]


def bad_batch_lines() -> list[str]:
    """A batch whose second half is structurally damaged.

    Lines 1–3 are valid applications; line 4 is invalid JSON; line 5 repeats
    line 2's application_id; line 6 has a non-object member.  Nothing after
    the bad line may be judged, and the run must be reported incomplete.
    """
    records = sample_applications()[:3]
    return [json.dumps(r, ensure_ascii=False) for r in records]


def build_eligibility_sample(workdir: str | Path,
                             write_static_files: bool = True) -> dict:
    workdir = Path(workdir)
    built = build_sample_workdir(workdir)
    v1 = v1_clause(built)
    if write_static_files:
        CLAUSE_DIR.mkdir(parents=True, exist_ok=True)
        (CLAUSE_DIR / "citations").mkdir(parents=True, exist_ok=True)
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        _write_json(CLAUSE_DIR / "E-9001-v1.json", v1)
        _write_json(CLAUSE_DIR / "citations" / "E-9001-to-public.json",
                    public_clause(built))
        _write_json(CLAUSE_DIR / "citations" / "E-9001-v2-as-v1.json",
                    v1_clause_v2_binding(built))
        _write_json(CLAUSE_DIR / "citations" / "E-9001-v1-shifted.json",
                    shifted_clause(built))
        with open(INPUT_DIR / "applications-basic.jsonl", "w",
                  encoding="utf-8") as handle:
            for record in sample_applications():
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"built": built, "clause": v1,
            "clause_path": CLAUSE_DIR / "E-9001-v1.json"}


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path
