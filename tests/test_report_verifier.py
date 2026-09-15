"""
tests/test_report_verifier.py —— core/report_verifier 离线单元测试(v1.6.0 防幻觉闭环)

覆盖:
    - extract_citations: 多种括号形式(【】/〔〕/()/（）/裸素材N)、带页码后缀
      [i] / [i,j]、多编号去重语义(提取保留顺序, 去重由 verify 完成)、无引用;
    - verify_report_citations: 全部有效 / 越界编号 / 零引用 / 空报告 / 空素材;
    - citation_check_mark: 三种文案形态(全部有效 / 有无效 / 零引用)。

说明: 纯函数测试, 不调用 LLM API / 不联网。
"""

from core.report_verifier import (
    citation_check_mark,
    extract_citations,
    verify_report_citations,
)


# =====================================================================
# extract_citations
# =====================================================================
def test_extract_supports_multiple_bracket_styles():
    """兼容报告可能使用的多种括号形式与裸编号。"""
    report = ("结论A【素材1】。结论B〔素材2〕。结论C(素材3)。"
              "结论D（素材4）。结论E素材5 结论F 素材 6。")
    assert extract_citations(report) == [1, 2, 3, 4, 5, 6]


def test_extract_keeps_order_and_duplicates():
    """提取保持出现顺序, 不去重(去重由 verify 完成)。"""
    report = "【素材3】【素材1】【素材3】【素材2】"
    assert extract_citations(report) == [3, 1, 3, 2]


def test_extract_supports_page_suffix():
    """带页码/条目后缀 [i] / [i,j] 只提取主编号。"""
    report = "（素材1[3]、素材4[2]）和素材8[1,5] 以及【素材10[2]】"
    assert extract_citations(report) == [1, 4, 8, 10]


def test_extract_does_not_split_multi_digit_numbers():
    """素材12 不能被误拆成 素材1 + 2(负向断言: 编号后不紧跟数字)。"""
    report = "【素材12】与【素材120】"
    assert extract_citations(report) == [12, 120]


def test_extract_empty_or_no_citations():
    assert extract_citations("") == []
    assert extract_citations("报告里没有任何引用标记") == []


# =====================================================================
# verify_report_citations
# =====================================================================
def test_verify_all_valid():
    """引用编号全部落在素材范围内 → all_valid=True。"""
    materials = ["素材A", "素材B", "素材C"]
    result = verify_report_citations("结论1【素材1】。结论2【素材3】。", materials)
    assert result["all_valid"] is True
    assert result["has_citations"] is True
    assert result["total_citations"] == 2
    assert result["unique_citations"] == [1, 3]
    assert result["invalid_citations"] == []
    assert result["materials_count"] == 3


def test_verify_catches_out_of_range():
    """引用素材4(实际只有 3 条) → invalid_citations=[4], all_valid=False。"""
    materials = ["素材A", "素材B", "素材C"]
    result = verify_report_citations("结论【素材1】。编造【素材4】。", materials)
    assert result["all_valid"] is False
    assert result["invalid_citations"] == [4]
    assert result["unique_citations"] == [1, 4]


def test_verify_no_citations():
    """报告无任何引用 → has_citations=False(提示重新生成)。"""
    materials = ["素材A", "素材B"]
    result = verify_report_citations("一份没有标注来源的报告。", materials)
    assert result["has_citations"] is False
    assert result["all_valid"] is False


def test_verify_empty_report_and_materials():
    assert verify_report_citations("", ["素材A"])["has_citations"] is False
    result = verify_report_citations("结论【素材1】", [])
    assert result["materials_count"] == 0
    assert result["invalid_citations"] == [1]   # 无素材时任何引用都越界


def test_verify_ignores_repeated_invalid():
    """同一越界编号重复出现只记一次(invalid_citations 去重)。"""
    materials = ["素材A"]
    result = verify_report_citations("【素材9】【素材9】【素材1】", materials)
    assert result["invalid_citations"] == [9]
    assert result["total_citations"] == 3


# =====================================================================
# citation_check_mark(UI 文案)
# =====================================================================
def test_mark_all_valid():
    result = verify_report_citations("结论【素材1】", ["素材A"])
    mark = citation_check_mark(result)
    assert mark.startswith("✅")
    assert "全部有效" in mark


def test_mark_with_invalid():
    result = verify_report_citations("【素材2】", ["素材A"])
    mark = citation_check_mark(result)
    assert mark.startswith("⚠️")
    assert "无效编号" in mark and "素材2" in mark


def test_mark_no_citations():
    result = verify_report_citations("无引用", ["素材A"])
    mark = citation_check_mark(result)
    assert mark.startswith("⚠️")
    assert "未包含任何素材引用" in mark
