class OracleVisibleToExecutionError(ValueError):
    """Raised when oracle/gold fields leak into execution."""


ORACLE_FIELD_NAMES = frozenset(
    {
        "golden_xlsx",
        "golden_response_path",
        "golden_file",
        "golden_path",
        "expected_new_values",
        "expected_modified_cells",
        "expected_new_value",
        "expected_old_value",
        "expected_answer_id",
        "expected_answer_text",
        "expected_cell",
        "expected_cells",
        "expected_unchanged_cells",
        "expected_preserved_formulas",
        "expected_preserved_styles",
        "oracle_config",
        "oracle_report",
        "oracle_spec",
        "oracle_spec_ref",
        "expected_output",
        "expected_output_ref",
        "expected_output_xlsx",
        "expected_output_sha256",
        "expected_output_canonical_sha256",
        "expected_output_file_sha256",
        "expected_formulas",
        "expected_final_cells",
        "expected_values",
        "gold_answers",
        "gold_answers_path",
        "answer_file",
        "answer_path",
        "answer_xlsx",
        "solution_file",
        "solution_path",
        "solution_xlsx",
    }
)
