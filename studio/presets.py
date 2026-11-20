PRESETS: dict = {
    # 1D (로프): IR 흡수·얇음 → depth coverage/overlap 매우 낮음 허용
    "1D": dict(sharp_rel_thr=0.3, depth_dropout_pct=0.20, residual_warn_mm=30.0,
               depth_coverage_pass_pct=50.0, depth_coverage_warn_pct=35.0,
               depth_coverage_tolerance_pct=10.0, depth_dropout_tolerance_pct=0.05,
               overlap_pass_pct=45.0, overlap_warn_pct=30.0, overlap_comp_pct=55.0),
    # 2D (천): 평면·IR 반사 불균일 → coverage/overlap 중간 허용
    "2D": dict(sharp_rel_thr=0.3, depth_dropout_pct=0.15, residual_warn_mm=25.0,
               depth_coverage_pass_pct=70.0, depth_coverage_warn_pct=55.0,
               depth_coverage_tolerance_pct=5.0, depth_dropout_tolerance_pct=0.05,
               overlap_pass_pct=65.0, overlap_warn_pct=50.0, overlap_comp_pct=75.0),
    # 3D (인형·rigid): 3D 구조 → coverage/overlap 높아야 함
    "3D": dict(sharp_rel_thr=0.4, depth_dropout_pct=0.05, residual_warn_mm=15.0,
               depth_coverage_pass_pct=80.0, depth_coverage_warn_pct=65.0,
               depth_coverage_tolerance_pct=3.0, depth_dropout_tolerance_pct=0.02,
               overlap_pass_pct=75.0, overlap_warn_pct=60.0, overlap_comp_pct=85.0),
}


