"""데이터 입력과 처리 배치 구성을 분리한다."""

from os import PathLike

from pandas import DataFrame, isna, read_csv
from pandas.api.types import is_scalar

from .contracts import AddressRecord, BatchConfig, BatchInput


def _text(value: object) -> str | None:
    if not is_scalar(value):
        raise ValueError("주소와 ID 컬럼은 행마다 단일 값을 가져야 합니다.")
    return None if isna(value) else str(value)


def load_dataset(
    dataset: str | PathLike[str] | DataFrame,
    *,
    address_column: str,
    id_column: str | None = None,
    encoding: str = "utf-8-sig",
) -> tuple[AddressRecord, ...]:
    """원본 순서를 유지하고 CSV에서 주소·ID를 문자열로 읽는다."""
    frame = dataset if isinstance(dataset, DataFrame) else read_csv(
        dataset, encoding=encoding, dtype=str, keep_default_na=False, skip_blank_lines=False,
    )
    required = [address_column] + ([id_column] if id_column is not None else [])
    for column in required:
        if list(frame.columns).count(column) != 1:
            raise ValueError(f"입력에 '{column}' 컬럼이 정확히 하나 있어야 합니다.")

    records = []
    seen = set()
    for index, values in enumerate(frame[required].itertuples(index=False, name=None)):
        address_id = f"row-{index + 1}" if id_column is None else _text(values[1])
        if address_id is None or not address_id.strip() or address_id in seen:
            raise ValueError(f"입력 {index + 1}행의 ID가 비어 있거나 중복됩니다.")
        seen.add(address_id)
        records.append(AddressRecord(address_id, _text(values[0])))
    return tuple(records)


def make_batches(
    records: tuple[AddressRecord, ...],
    *,
    run_id: str,
    config: BatchConfig,
    batch_size: int | None = None,
) -> tuple[BatchInput, ...]:
    """지정한 크기로 나누며, 생략하면 전체를 한 배치로 묶는다."""
    if batch_size is None:
        return (BatchInput(run_id, records, config),)
    if batch_size < 1:
        raise ValueError("batch_size는 양수여야 합니다.")
    return tuple(
        BatchInput(f"{run_id}:{index // batch_size + 1}", records[index:index + batch_size], config)
        for index in range(0, len(records), batch_size)
    )
