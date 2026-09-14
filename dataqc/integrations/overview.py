"""Read camera measurements from both historical and shared QC reports."""


def integrate_overview(app):
    old = app.qc_overview_record_from_json

    def record(episode_id, report_path, report_cache=None):
        result = old(episode_id, report_path, report_cache)
        if result is None:
            return result
        report = app.load_qc_report(report_path, report_cache)
        # Use the same confirmed Action closure events as numerical QC. Simulation
        # may copy Action into State, so it must not be counted as physical feedback.
        for check in report.get('checks', []):
            if (check.get('name') or check.get('key')) != 'gripper_sequence':
                continue
            channels = (check.get('detail') or {}).get('channels') or {}
            for hand in ('left', 'right'):
                events = (channels.get(hand) or {}).get('action_close_frames')
                if isinstance(events, list) and all(type(v) is int and v >= 0 for v in events):
                    result[f'{hand}_gripper_close_events'] = len(events)
        if result.get('camera_view_count') is not None:
            return result
        summary = report.get('summary') or {}
        counts = summary.get('camera_counts')
        if not isinstance(counts, dict) or not counts:
            return result
        measured = {name: app.as_int(value) for name, value in counts.items()}
        if any(value is None or value < 0 for value in measured.values()):
            return result
        result.update(
            camera_view_count=sum(value > 0 for value in measured.values()),
            camera_expected_view_count=len(measured),
            camera_detail='；'.join(f'{name}: {value}帧' for name, value in measured.items()),
        )
        return result

    app.qc_overview_record_from_json = record
    old_js = '''    function finiteNumber(value) {
      const number = Number(value);'''
    new_js = '''    function finiteNumber(value) {
      if (value === null || value === undefined || typeof value === "boolean" || (typeof value === "string" && value.trim() === "")) return null;
      const number = Number(value);'''
    if old_js not in app.HTML:
        raise RuntimeError('Overview finiteNumber integration anchor missing')
    app.HTML = app.HTML.replace(old_js, new_js)
    app.HTML = app.HTML.replace('          gripperCloseGroupedBarChart(records),', '''          targetValuePieChart(records, "left_gripper_close_events", "左夹爪闭合（标准 1 次）", 1, "次"),
          targetValuePieChart(records, "right_gripper_close_events", "右夹爪闭合（标准 1 次）", 1, "次"),
          gripperCloseGroupedBarChart(records),''')
