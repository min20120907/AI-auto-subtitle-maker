#!/usr/bin/env python3
"""
Unit and regression tests for VTuber side comments separation, dedup, and spatial layout:
1. fast_ocr_keyframes: dedup_cross_reader_boxes, merge_same_line_boxes, filter_and_separate_regions
2. agentic_full_translate: 9s sliding window dedup, dialogue matching without single-line collapse
3. generate_context_ass & webui_server: multi-region independent positioning & vertical decollision
"""
import os
import sys
from pathlib import Path

REAL_WORK_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REAL_WORK_DIR))

os.environ["CUDA_VISIBLE_DEVICES"] = ""

import fast_ocr_keyframes
import agentic_full_translate
import generate_context_ass
import webui_server


def test_ocr_filter_and_dedup_cross_reader():
    """Verify dedup_cross_reader_boxes removes overlapping multi-reader boxes."""
    raw = [
        {"text": "안녕하세요", "x_min": 100, "x_max": 200, "y_min": 50, "y_max": 80, "w": 100, "h": 30, "cx": 150, "cy": 65},
        {"text": "アンニョン", "x_min": 102, "x_max": 198, "y_min": 51, "y_max": 79, "w": 96, "h": 28, "cx": 150, "cy": 65},
    ]
    deduped = fast_ocr_keyframes.dedup_cross_reader_boxes(raw)
    assert len(deduped) == 1, f"Expected 1 deduped box, got {len(deduped)}"


def test_ocr_merge_same_line_boxes():
    """Verify merge_same_line_boxes only merges horizontal neighbors and never vertical lines."""
    boxes = [
        # Line 1
        {"text": "Hello", "x_min": 100, "x_max": 150, "y_min": 100, "y_max": 130, "w": 50, "h": 30, "cx": 125, "cy": 115},
        {"text": "World", "x_min": 160, "x_max": 220, "y_min": 102, "y_max": 132, "w": 60, "h": 30, "cx": 190, "cy": 117},
        # Line 2 (vertical below, should NOT merge)
        {"text": "SecondLine", "x_min": 100, "x_max": 250, "y_min": 160, "y_max": 190, "w": 150, "h": 30, "cx": 175, "cy": 175},
    ]
    merged = fast_ocr_keyframes.merge_same_line_boxes(boxes)
    assert len(merged) == 2, f"Expected 2 lines, got {len(merged)}"
    assert "Hello World" in merged[0]["text"]
    assert "SecondLine" in merged[1]["text"]


def test_ocr_filter_and_separate_regions_and_max_comments():
    """Verify filter_and_separate_regions separates side comments and caps at max_side_comments."""
    lines = [
        # Center card (width > 600 or central position)
        {"text": "Main Event Announcement", "cx": 960, "cy": 300, "w": 700, "h": 50, "x_min": 610, "x_max": 1310, "y_min": 275, "y_max": 325},
        # Side comments on right edge (cx > 1350, h=40 >= 36 so not filtered as tiny chat)
        {"text": "Viewer A: grassss", "cx": 1600, "cy": 200, "w": 200, "h": 40, "x_min": 1500, "x_max": 1700, "y_min": 180, "y_max": 220},
        {"text": "Viewer B: LOL 777", "cx": 1600, "cy": 250, "w": 180, "h": 40, "x_min": 1510, "x_max": 1690, "y_min": 230, "y_max": 270},
        {"text": "Viewer C: so cute!!", "cx": 1600, "cy": 300, "w": 210, "h": 40, "x_min": 1495, "x_max": 1705, "y_min": 280, "y_max": 320},
        {"text": "Viewer D: spam 444", "cx": 1600, "cy": 350, "w": 200, "h": 40, "x_min": 1500, "x_max": 1700, "y_min": 330, "y_max": 370},
    ]
    res = fast_ocr_keyframes.filter_and_separate_regions(lines, max_side_comments=2)
    # 1 main card + 2 side comments = 3 items total
    assert len(res) == 3, f"Expected 3 items (1 card + 2 comments), got {len(res)}"
    side_items = [r for r in res if r.get("is_side_comment")]
    assert len(side_items) == 2, f"Expected 2 side comments, got {len(side_items)}"


def test_agentic_sliding_window_dedup():
    """Verify 9-second sliding window deduplication in agentic_full_translate."""
    dialogues = [
        {"id": "1", "start_sec": 10.0, "end_sec": 12.0, "text": "대사 1"},
        {"id": "2", "start_sec": 13.0, "end_sec": 15.0, "text": "대사 2"},
        {"id": "3", "start_sec": 25.0, "end_sec": 27.0, "text": "대사 3"},
    ]
    ocr_cards = [
        {"sec": 10.5, "text": "Same Side Comment", "items": [{"text": "Same Side Comment", "x": 1600, "y": 200}]},
        {"sec": 13.5, "text": "Same Side Comment", "items": [{"text": "Same Side Comment", "x": 1600, "y": 200}]},  # within 9s: should NOT attach again
        {"sec": 25.5, "text": "Same Side Comment", "items": [{"text": "Same Side Comment", "x": 1600, "y": 200}]},  # > 9s later: can attach
    ]
    res = agentic_full_translate.attach_cards_to_dialogues(dialogues, ocr_cards, rolling_dedup_window_sec=9.0)
    assert len(res["1"]) == 1
    assert len(res["2"]) == 0, "Comment repeated within 9s sliding window should be deduplicated"
    assert len(res["3"]) == 1, "Comment appearing after 9s window can attach"


def test_spatial_layout_decollision():
    """Verify generate_context_ass and webui_server decollide overlapping cards vertically."""
    sec = 10.0
    cards = ["Comment Top", "Comment Bottom"]
    ocr_cards = [
        {
            "sec": 10.0,
            "x": 1600,
            "y": 500,
            "items": [
                {"text": "Comment Top", "x": 1600, "y": 500, "is_side_comment": True},
                {"text": "Comment Bottom", "x": 1600, "y": 510, "is_side_comment": True}, # Colliding Y
            ]
        }
    ]
    matched = webui_server._match_cards_spatial(sec, cards, ocr_cards, card_size=38)
    assert len(matched) == 2
    # Ensure vertical distance is separated by at least card_size + margin
    y_diff = abs(matched[0]["y"] - matched[1]["y"])
    assert y_diff >= 45, f"Expected decollided vertical gap >= 45px, got {y_diff}"


if __name__ == "__main__":
    test_ocr_filter_and_dedup_cross_reader()
    test_ocr_merge_same_line_boxes()
    test_ocr_filter_and_separate_regions_and_max_comments()
    test_agentic_sliding_window_dedup()
    test_spatial_layout_decollision()
    print("ALL VTUBER SIDE COMMENT TESTS PASSED!")
