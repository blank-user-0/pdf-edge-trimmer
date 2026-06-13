import tempfile
import unittest
from pathlib import Path

import fitz

from pdf_edge_trimmer.app import trim_pdf_edges_by_mode


class PdfEdgeTrimmerTests(unittest.TestCase):
    def make_sample_pdf(self, path: Path) -> None:
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((250, 396), "KEEP CENTER CONTENT", fontsize=18)
        page.insert_text((250, 24), "REMOVE_TOP_EDGE", fontsize=14)
        page.insert_text((250, 780), "REMOVE_BOTTOM_EDGE", fontsize=14)
        page.insert_text((8, 396), "REMOVE_LEFT_EDGE", fontsize=14, rotate=90)
        page.insert_text((604, 396), "REMOVE_RIGHT_EDGE", fontsize=14, rotate=270)
        doc.save(path)
        doc.close()

    def assert_trimmed_size(self, path: Path) -> str:
        doc = fitz.open(path)
        page = doc.load_page(0)
        self.assertEqual(doc.page_count, 1)
        self.assertEqual(round(page.rect.width, 2), 576.0)
        self.assertEqual(round(page.rect.height, 2), 720.0)
        text = page.get_text()
        doc.close()
        return text

    def test_redact_mode_keeps_center_text_and_removes_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.pdf"
            out = Path(tmp) / "redacted.pdf"
            self.make_sample_pdf(src)

            trim_pdf_edges_by_mode(
                src,
                out,
                top_inches=0.5,
                bottom_inches=0.5,
                left_inches=0.25,
                right_inches=0.25,
                output_mode="redact",
                overwrite=True,
            )

            text = self.assert_trimmed_size(out)
            self.assertIn("KEEP CENTER CONTENT", text)
            self.assertNotIn("REMOVE_TOP_EDGE", text)
            self.assertNotIn("REMOVE_BOTTOM_EDGE", text)
            self.assertNotIn("REMOVE_LEFT_EDGE", text)
            self.assertNotIn("REMOVE_RIGHT_EDGE", text)

    def test_compressed_mode_rebuilds_as_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.pdf"
            out = Path(tmp) / "compressed.pdf"
            self.make_sample_pdf(src)

            trim_pdf_edges_by_mode(
                src,
                out,
                top_inches=0.5,
                bottom_inches=0.5,
                left_inches=0.25,
                right_inches=0.25,
                output_mode="compressed",
                jpeg_quality=60,
                overwrite=True,
            )

            text = self.assert_trimmed_size(out)
            self.assertNotIn("KEEP CENTER CONTENT", text)
            doc = fitz.open(out)
            self.assertEqual(len(doc.load_page(0).get_images(full=True)), 1)
            doc.close()

    def test_crop_mode_changes_visible_page_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.pdf"
            out = Path(tmp) / "cropped.pdf"
            self.make_sample_pdf(src)

            trim_pdf_edges_by_mode(
                src,
                out,
                top_inches=0.5,
                bottom_inches=0.5,
                left_inches=0.25,
                right_inches=0.25,
                output_mode="crop",
                overwrite=True,
            )

            self.assert_trimmed_size(out)


if __name__ == "__main__":
    unittest.main()
