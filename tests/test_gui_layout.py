"""GUI 레이아웃 회귀: 좁은 창에서도 하단 액션 툴바가 잘리지 않는다.

pack은 선언 순서로 공간을 떼어 가므로 expand=True인 파일 목록을 먼저 배치하면
마지막에 배치된 툴바가 화면 밖으로 밀린다(높은 DPI 배율의 노트북에서 업로드 버튼이
보이지 않는 증상). 하단 고정 바를 먼저 배치해 공간을 선점하는지 검증한다.

Tk root는 테스트당 재생성하면 테마·이미지 리소스가 이전 root에 묶여 실패하므로
한 창에서 여러 크기를 확인한다.
"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")


def test_bottom_bars_survive_small_windows():
    """최소 크기·그보다 짧은 창에서도 업로드 버튼과 상태바가 배치된다."""
    from stardustlib.gui.app import StardustApp

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("디스플레이가 없는 환경")
    try:
        app = StardustApp(root, None)  # 설정 없이도 위젯은 모두 생성된다
        for size in ("760x480", "760x400", "900x620"):
            root.geometry(size)
            root.update()
            assert app.upload_btn.winfo_ismapped(), f"업로드 버튼 잘림: {size}"
            assert app.status.winfo_ismapped(), f"상태바 잘림: {size}"
            # 버튼 하단이 창 안에 들어와야 한다(잘리면 창 높이를 넘는다).
            bottom = (
                app.upload_btn.winfo_rooty() + app.upload_btn.winfo_height()
            )
            assert bottom <= root.winfo_rooty() + root.winfo_height(), size
    finally:
        root.destroy()
