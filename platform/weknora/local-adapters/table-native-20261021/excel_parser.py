"""
Excel Parser Module

This module provides functionality to parse Excel files (.xlsx, .xls) into
structured Document objects with text content and chunks. It supports multiple
sheets and handles various Excel formats using pandas.
"""
import logging
import re
from io import BytesIO
from typing import Any, List

import pandas as pd

from docreader.models.document import Chunk, Document
from docreader.parser.base_parser import BaseParser
from docreader.parser.excel_convert import (
    convert_excel_to_xlsx_bytes,
    detect_excel_format,
    engine_for_format,
    normalize_excel_bytes,
)
from docreader.parser.xlsx_merge import fill_merged_cells_xlsx
from docreader.parser.xlsx_repair import repair_xlsx_bytes

logger = logging.getLogger(__name__)

# Pattern to detect Excel image function strings that should be excluded from
# parsed text content.  WPS uses =DISPIMG("ID",mode) to embed images in cells;
# when opened by other tools the formula may appear as plain text prefixed with
# "_xlfn." or "=".  Office 365 uses =_xlfn.IMAGE(url, ...) similarly.
# The _xlfn. prefix is optional — WPS may omit it (e.g. =DISPIMG("ID",1)).
_IMAGE_FUNC_RE = re.compile(
    r"^=?(_xlfn\.)?(DISPIMG|IMAGE)\(", re.IGNORECASE
)


def _is_image_function(value: object) -> bool:
    """Return True if *value* looks like an embedded-image function string."""
    if not isinstance(value, str):
        return False
    return _IMAGE_FUNC_RE.match(value) is not None


class ExcelParser(BaseParser):
    """Parser for Excel files (.xlsx, .xls).
    
    This parser extracts text content from Excel files by processing all sheets
    and converting each row into a structured text format. Each row becomes a
    separate chunk with key-value pairs.
    
    Features:
        - Supports multiple sheets in a single Excel file
        - Automatically removes completely empty rows
        - Converts each row to "column: value" format
        - Creates individual chunks for each row for better granularity
        
    Example:
        >>> parser = ExcelParser()
        >>> with open("data.xlsx", "rb") as f:
        ...     content = f.read()
        ...     document = parser.parse_into_text(content)
        >>> print(document.content)
        Name: John,Age: 30,City: NYC
        Name: Jane,Age: 25,City: LA
    """

    def __init__(
        self,
        file_name: str = "",
        file_type: str | None = None,
        xlsx_first_row_as_header: Any = False,
        **kwargs: Any,
    ):
        super().__init__(file_name=file_name, file_type=file_type, **kwargs)
        self.xlsx_first_row_as_header = _parse_bool(xlsx_first_row_as_header)
    
    def parse_into_text(self, content: bytes) -> Document:
        """Parse Excel file bytes into a Document object.
        
        Args:
            content: Raw bytes of the Excel file
            
        Returns:
            Document: Parsed document containing:
                - content: Full text with all rows from all sheets
                - chunks: List of Chunk objects, one per row
                
        Note:
            - Empty rows (all NaN values) are automatically skipped
            - Each row is formatted as: "col1: val1,col2: val2,..."
            - Chunks maintain sequential ordering across all sheets
        """
        chunks: List[Chunk] = []
        text: List[str] = []
        start, end = 0, 0

        excel_file = _open_excel_file(content, file_type=self.file_type)
        
        # Process each sheet in the Excel file
        for excel_sheet_name in excel_file.sheet_names:
            df = _read_sheet_dataframe(
                excel_file,
                excel_sheet_name,
                xlsx_first_row_as_header=self.xlsx_first_row_as_header,
            )
            # Sheet names are source data, including headers for empty named sheets.
            heading = "工作表：" + str(excel_sheet_name) + "\n"
            text.append(heading)
            end += len(heading)
            start = end
            # Remove rows where all values are NaN (completely empty rows)
            df.dropna(how="all", inplace=True)

            # Process each row in the DataFrame
            for _, row in df.iterrows():
                page_content = []
                # Build key-value pairs for non-null values
                for k, v in row.items():
                    if pd.notna(v) and not _is_image_function(v):
                        page_content.append(f"{k}: {v}")
                
                # Skip rows with no valid content
                if not page_content:
                    continue
                
                # Format row as comma-separated key-value pairs
                content_row = ",".join(page_content) + "\n"
                end += len(content_row)
                text.append(content_row)
                
                # Create a chunk for this row with position tracking
                chunks.append(
                    Chunk(content=content_row, seq=len(chunks), start=start, end=end)
                )
                start = end

        # Preserve saved WPS checkbox values without executing any control or binding.
        if content.startswith(b"PK\x03\x04"):
            import zipfile, json
            from docreader.parser.wps_checkboxes import freeze
            with zipfile.ZipFile(BytesIO(content)) as archive:
                _, _, notes = freeze(archive)
            for note in notes:
                for line in note['native_body'].splitlines()[1:]:
                    record = json.loads(line)
                    saved = 'true' if record['checked'] else 'false'
                    native = 'WPS_CHECKBOX ' + record['part'] + ' saved=' + saved + ' ' + line + '\n'
                    text.append(native)
                    chunks.append(Chunk(content=native, seq=len(chunks), start=end, end=end+len(native)))
                    end += len(native)
                    start = end
        # Combine all text and return as Document
        return Document(content="".join(text), chunks=chunks)


def _read_sheet_dataframe(
    excel_file: pd.ExcelFile,
    sheet_name: str,
    xlsx_first_row_as_header: bool = False,
) -> pd.DataFrame:
    """Read a worksheet into a DataFrame with stable column labels."""
    from openpyxl.utils import get_column_letter

    # Keep row 1 as data by default for both XLSX and legacy XLS. Users can
    # explicitly restore the historical behavior where row 1 supplies semantic
    # labels for every row.
    df = excel_file.parse(sheet_name=sheet_name, header=None, dtype=object, keep_default_na=False, na_values=[""])
    # pandas maps Excel error cells to NaN. Keep their saved error text so
    # financial worksheets do not silently lose #VALUE! / #DIV/0! evidence.
    book = excel_file.book
    errors = []
    if hasattr(book, "sheet_by_name"):
        import xlrd
        sheet = book.sheet_by_name(sheet_name)
        for row in range(sheet.nrows):
            for col in range(sheet.ncols):
                if sheet.cell_type(row, col) == xlrd.XL_CELL_ERROR:
                    errors.append((row, col, xlrd.error_text_from_code[sheet.cell_value(row, col)]))
    elif hasattr(book, "worksheets"):
        for row in book[sheet_name].iter_rows():
            for cell in row:
                if cell.data_type == "e":
                    errors.append((cell.row - 1, cell.column - 1, str(cell.value)))
    if errors:
        df = df.reindex(index=range(max(len(df.index), max(r for r, c, v in errors) + 1)),
                        columns=range(max(len(df.columns), max(c for r, c, v in errors) + 1))).astype(object)
        for row, col, value in errors:
            df.iat[row, col] = value
    if xlsx_first_row_as_header and len(df.index) >= 2:
        df.columns = _stable_header_labels(df.iloc[0].tolist())
        return df.iloc[1:].copy()

    df.columns = [get_column_letter(idx + 1) for idx in range(len(df.columns))]
    return df


def _stable_header_labels(values: List[object]) -> List[str]:
    """Build non-empty, unique labels from an explicitly selected header row."""
    from openpyxl.utils import get_column_letter

    labels: List[str] = []
    counts: dict[str, int] = {}
    for index, value in enumerate(values, start=1):
        label = ""
        if pd.notna(value) and not _is_image_function(value):
            label = str(value).strip()
        if not label:
            label = get_column_letter(index)

        count = counts.get(label, 0) + 1
        counts[label] = count
        labels.append(label if count == 1 else f"{label}_{count}")
    return labels


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _prepare_xlsx_bytes(data: bytes) -> bytes:
    # Some WPS exports contain <fill/>. Keep style indexes and date formats;
    # normalize only those empty fill entries in the reader's memory copy.
    if data.startswith(b"PK\x03\x04"):
        import zipfile
        from xml.etree import ElementTree as ET
        with zipfile.ZipFile(BytesIO(data)) as archive:
            if 'xl/styles.xml' in archive.namelist():
                styles = ET.fromstring(archive.read('xl/styles.xml'))
                fills = styles.find('{*}fills')
                empty = [] if fills is None else [fill for fill in fills if len(fill) == 0]
                if empty:
                    for fill in empty:
                        ET.SubElement(fill, fill.tag.replace('}fill', '}patternFill'), {'patternType': 'none'})
                    result = BytesIO()
                    with zipfile.ZipFile(result, 'w') as fixed:
                        for item in archive.infolist():
                            fixed.writestr(item, ET.tostring(styles, encoding='utf-8', xml_declaration=True) if item.filename == 'xl/styles.xml' else archive.read(item.filename))
                    data = result.getvalue()
    repaired = repair_xlsx_bytes(data)
    if repaired is not None:
        data = repaired
    return fill_merged_cells_xlsx(data)


def _open_excel_file(content: bytes, file_type: str | None = None) -> pd.ExcelFile:
    """Open an Excel workbook with explicit engine selection and fallbacks."""
    data = content
    converted_via_soffice = False

    while True:
        ext = detect_excel_format(data)
        if ext is None:
            if converted_via_soffice:
                raise ValueError(
                    "Excel file format cannot be determined, you must specify an engine manually."
                )
            try:
                data = normalize_excel_bytes(data, file_type=file_type)
            except ValueError as exc:
                raise ValueError(
                    "Excel file format cannot be determined, you must specify an engine manually."
                ) from exc
            converted_via_soffice = True
            continue

        if ext == "ods":
            converted = convert_excel_to_xlsx_bytes(data, suffix=".ods")
            if converted:
                data = converted
                continue

        engine = engine_for_format(ext)
        if ext == "xlsx":
            data = _prepare_xlsx_bytes(data)
            engine = "openpyxl"
        try:
            return pd.ExcelFile(BytesIO(data), engine=engine)
        except ImportError as exc:
            raise ValueError(
                f"Excel engine {engine!r} is not available for .{ext} files"
            ) from exc
        except KeyError as exc:
            if "sharedStrings.xml" not in str(exc) or engine != "openpyxl":
                raise
            repaired = repair_xlsx_bytes(data)
            if repaired is None:
                raise
            logger.info("Repaired XLSX sharedStrings packaging before parse")
            data = _prepare_xlsx_bytes(repaired)
            continue
        except ValueError as exc:
            if converted_via_soffice or "cannot be determined" not in str(exc):
                raise
            try:
                data = normalize_excel_bytes(content, file_type=file_type)
            except ValueError:
                raise
            converted_via_soffice = True
            continue


if __name__ == "__main__":
    # Example usage: Parse an Excel file and display results
    logging.basicConfig(level=logging.DEBUG)

    # Specify the path to your Excel file
    your_file = "/path/to/your/file.xlsx"
    parser = ExcelParser()
    
    # Read and parse the Excel file
    with open(your_file, "rb") as f:
        content = f.read()
        document = parser.parse_into_text(content)
        
        # Display the full document content
        logger.error(document.content)

        # Display the first chunk as an example
        for chunk in document.chunks:
            logger.error(chunk.content)
            break  # Only show the first chunk
