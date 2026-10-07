from __future__ import annotations

from pathlib import Path

import pytest

from corecycler.monitor.hwmon import SensorRead, Sensors


def hwmon(root: Path, name: str, files: dict[str, str], bus: str | None = None) -> Path:
    device = root / "class" / f"hwmon{len(list((root / 'class').glob('hwmon*')))}"
    device.mkdir(parents=True)
    (device / "name").write_text(name + "\n")
    for file, text in files.items():
        (device / file).write_text(text + "\n")
    if bus is not None:
        (root / "bus" / bus).mkdir(parents=True)
        (device / "device").symlink_to(root / "bus" / bus)
    return device


K10 = {
    "temp1_label": "Tctl",
    "temp1_input": "71000",
    "temp3_label": "Tccd1",
    "temp3_input": "74000",
    "temp4_label": "Tccd2",
    "temp4_input": "54000",
}


def test_tctl_and_each_ccd_keep_their_own_names_and_the_stop_sees_the_hottest(tmp_path):
    hwmon(tmp_path, "k10temp", K10)
    read = Sensors(tmp_path / "class").read()
    assert (read.tctl, read.tccd, read.hottest) == (71.0, ((0, 74.0), (1, 54.0)), 74.0)


def test_the_board_and_gpu_voltages_are_read_under_their_sources_and_a_failed_read_is_none(tmp_path):
    hwmon(tmp_path, "zenpower", {**K10, "in1_label": "SVI2_Core", "in1_input": "1100"}, "0000:00:18.3")
    superio = hwmon(tmp_path, "nct6799", {"in0_input": "712", "in10_input": "1056"}, "nct6775.656")
    gpu = {"in0_label": "vddgfx", "in0_input": "595", "in1_label": "vddnb", "in1_input": "1250"}
    hwmon(tmp_path, "amdgpu", gpu, "0000:7c:00.0")
    hwmon(tmp_path, "amdgpu", {"in0_label": "vddgfx", "in0_input": "969"}, "0000:03:00.0")
    hwmon(tmp_path, "hidpp_battery_0", {"in0_input": "4125"})
    hwmon(tmp_path, "nvme", {"temp1_input": "40000"})
    sensors = Sensors(tmp_path / "class")
    assert sensors.read().volts == (
        ("amdgpu@0000:03:00.0:vddgfx", 0.969),
        ("amdgpu@0000:7c:00.0:vddgfx", 0.595),
        ("amdgpu@0000:7c:00.0:vddnb", 1.25),
        ("nct6799:in0", 0.712),
        ("nct6799:in10", 1.056),
        ("zenpower:SVI2_Core", 1.1),
    )
    (superio / "in10_input").write_text("garbage\n")
    (superio / "in0_input").unlink()
    assert sensors.read().volts[3:5] == (("nct6799:in0", None), ("nct6799:in10", None))


def test_an_unreadable_temperature_is_none_and_the_hottest_comes_from_what_was_read(tmp_path):
    device = hwmon(tmp_path, "k10temp", K10)
    sensors = Sensors(tmp_path / "class")
    (device / "temp3_input").write_text("garbage\n")
    read = sensors.read()
    assert (read.tctl, read.tccd, read.hottest) == (71.0, ((0, None), (1, 54.0)), 71.0)


def test_a_machine_without_a_cpu_sensor_reads_nothing_and_says_so(tmp_path):
    hwmon(tmp_path, "nvme", {"temp1_input": "40000"})
    for root in (tmp_path / "class", tmp_path / "missing"):
        sensors = Sensors(root)
        assert sensors.available() is False
        assert sensors.read() == SensorRead()


def test_an_amd_driver_wins_over_coretemp_and_an_unlabelled_first_input_is_tctl(tmp_path):
    hwmon(tmp_path, "coretemp", {"temp1_label": "Package id 0", "temp1_input": "90000"})
    hwmon(tmp_path, "k10temp", {"temp1_input": "61000", "temp2_input": "65000"})
    read = Sensors(tmp_path / "class").read()
    assert (read.tctl, read.hottest) == (61.0, 65.0)


def test_a_read_never_scans_the_tree_again(tmp_path, monkeypatch):
    hwmon(tmp_path, "k10temp", K10)
    sensors = Sensors(tmp_path / "class")

    def scanned(*_args, **_kwargs):
        pytest.fail("a read scanned hwmon again")

    monkeypatch.setattr(Path, "iterdir", scanned)
    monkeypatch.setattr(Path, "glob", scanned)
    assert sensors.read().hottest == 74.0


def test_a_nameless_node_or_a_name_that_is_no_file_is_skipped(tmp_path):
    (tmp_path / "class" / "hwmon0").mkdir(parents=True)
    (tmp_path / "class" / "hwmon1" / "name").mkdir(parents=True)
    hwmon(tmp_path, "k10temp", {"temp1_input": "51000"})
    assert Sensors(tmp_path / "class").read().hottest == 51.0


def test_an_unreadable_label_is_logged_and_its_input_still_counts_toward_the_hottest(tmp_path, monkeypatch, caplog):
    hwmon(tmp_path, "k10temp", K10)
    read_text = Path.read_text

    def refusing(path, *args, **kwargs):
        if path.name == "temp3_label":
            raise PermissionError("denied")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refusing)
    with caplog.at_level("WARNING", logger="corecycler.monitor.hwmon"):
        read = Sensors(tmp_path / "class").read()
    assert "temp3_label" in caplog.text and "denied" in caplog.text
    assert (read.tctl, read.tccd, read.hottest) == (71.0, ((1, 54.0),), 74.0)


def vcore_of(tmp_path, *devices) -> float | None:
    for name, files in devices:
        hwmon(tmp_path, name, files)
    sensors = Sensors(tmp_path / "class")
    return sensors.vcore(sensors.read())


def test_vcore_is_the_cpu_driver_s_own_reading_before_any_board_chip(tmp_path):
    got = vcore_of(
        tmp_path,
        ("k10temp", {"temp1_input": "60000", "in0_label": "Vcore", "in0_input": "1250"}),
        ("it8688", {"in0_input": "999", "in4_label": "CPU Vcore", "in4_input": "1300"}),
    )
    assert got == 1.25


def test_vcore_is_a_board_chip_s_labelled_input_before_its_in0(tmp_path):
    got = vcore_of(
        tmp_path,
        ("zenpower5", {"temp1_input": "60000"}),
        ("nct6687", {"in0_label": "+12V", "in0_input": "12340", "in4_label": "CPU Vcore", "in4_input": "1400"}),
    )
    assert got == 1.4


@pytest.mark.parametrize("chip", ["nct6683", "nct6686", "nct6687", "nct6795", "it8688"])
def test_vcore_falls_back_to_a_board_chip_s_in0(tmp_path, chip):
    got = vcore_of(
        tmp_path,
        ("zenpower5", {"temp1_input": "60000"}),
        (chip, {"in0_label": "VIN0", "in0_input": "1300", "in1_label": "+3.3V", "in1_input": "3300"}),
    )
    assert got == 1.3


def test_no_core_rail_reading_is_none(tmp_path):
    got = vcore_of(
        tmp_path, ("k10temp", {"temp1_input": "60000"}), ("amdgpu", {"in0_label": "vddgfx", "in0_input": "900"})
    )
    assert got is None
