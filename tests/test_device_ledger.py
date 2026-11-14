"""The device list is read as the user meant it, or refused with the line.

Every case here is a ledger that used to load as something else: another
device, other ports, a missing device or a traceback.
"""

import pytest

from netdiag.cli import main
from netdiag.core import InputError
from netdiag.inventory import (
    InventoryError,
    load_devices,
    parse_csv_text,
    parse_structured,
    parse_target_spec,
)


def _rows(devices):
    return [(d.name, d.host, d.ports) for d in devices]


def _write(tmp_path, name, text, encoding="cp932"):
    path = tmp_path / name
    path.write_bytes(text.replace("\n", "\r\n").encode(encoding))
    return path


@pytest.mark.parametrize(
    "written, host",
    [
        ("192.168.001.010", "192.168.1.10"),
        ("010.001.001.001", "10.1.1.1"),
        ("192.168.033.002", "192.168.33.2"),
        ("192.168.33.0254", "192.168.33.254"),
        ("１９２．１６８．３．３９", "192.168.3.39"),
    ],
)
def test_zero_padded_ipv4_is_read_as_decimal_everywhere(tmp_path, written, host):
    csv_dev = parse_csv_text(f"名前,IPアドレス\nPLC1,{written}\n")[0]
    cli_dev = parse_target_spec(f"PLC1={written}:502")
    yaml_dev = load_devices(
        _write(tmp_path, "l.yaml", f"devices:\n  - {{name: PLC1, host: {written}}}\n")
    )[0]
    assert csv_dev.host == cli_dev.host == yaml_dev.host == host
    assert parse_target_spec(written).name == host


@pytest.mark.parametrize(
    "written", ["192.168.1.300", "192.168.33.0376", "192.168.1", "192.168..1"]
)
def test_a_broken_dotted_address_is_refused(written):
    with pytest.raises(
        InventoryError, match=f"line 2: not a valid IPv4 address: {written}"
    ):
        parse_csv_text(f"name,host\nPLC,{written}\n")
    with pytest.raises(InventoryError, match="not a valid IPv4 address"):
        parse_target_spec(written)


def test_names_starting_with_hash_are_devices_and_comments_are_not():
    devs = parse_csv_text(
        "# 第1ライン\n"
        "名前,IPアドレス,ポート\n"
        "#1プレス,192.0.2.10,502\n"
        "#192.0.2.11,PLC-old,502\n"
        "# PLC0,192.0.2.12,502\n"
        "#2号機,192.0.2.13,\n"
        "#メモ\n"
    )
    assert _rows(devs) == [
        ("#1プレス", "192.0.2.10", [502]),
        ("#2号機", "192.0.2.13", []),
    ]


def test_a_header_with_a_hash_row_number_column_is_the_header():
    devs = parse_csv_text(
        "# 名前,IPアドレス,ポート の順に書く\n"
        "#,名前,IPアドレス,ライン番号,ポート\n"
        "1,PLC-1,192.0.2.39,2,502\n"
        "#,PLC-old,192.0.2.38,2,502\n"
        "2,HMI-1,192.0.2.40,3,\n"
    )
    assert _rows(devs) == [
        ("PLC-1", "192.0.2.39", [502]),
        ("HMI-1", "192.0.2.40", []),
    ]


def test_headerless_rows_take_ports_only_after_the_address():
    devs = parse_csv_text(
        "1,ゲートウェイ,192.0.2.1\n"
        "2,PLC1,192.0.2.39,modbus\n"
        "PLC2,192.0.2.40,opcua;502\n"
        "192.0.2.41,HMI,80\n"
    )
    assert _rows(devs) == [
        ("ゲートウェイ", "192.0.2.1", []),
        ("PLC1", "192.0.2.39", [502]),
        ("PLC2", "192.0.2.40", [4840, 502]),
        ("HMI", "192.0.2.41", [80]),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "第1ライン 機器台帳,,,\n名前,IPアドレス,ポート,備考\nPLC1,192.0.2.39,502,\n",
        ",,,\n名前,IPアドレス,ポート,備考\nPLC1,192.0.2.39,502,\n",
        "名前,ＩＰアドレス,ポート\nPLC1,192.0.2.39,502\n",
        "名前,IPv4アドレス,使用ポート\nPLC1,192.0.2.39,502\n",
        "Name,IP,Port No.\nPLC1,192.0.2.39,502\n",
        "Name,IP,TCP Ports\nPLC1,192.0.2.39,502\n",
        "名前,IPアドレス,ポート(TCP)\nPLC1,192.0.2.39,502\n",
        "名前,IPアドレス,ポート\nPLC1,192.0.2.39,５０２\n",
    ],
)
def test_headers_are_found_below_titles_and_in_any_width(text):
    assert _rows(parse_csv_text(text)) == [("PLC1", "192.0.2.39", [502])]


@pytest.mark.parametrize(
    "header, row",
    [
        ("名前,IPアドレス(PLC),ポート", "PLC1,192.0.2.39,502"),
        ("名前,ＩＰアドレス（ＰＬＣ）,ポート", "PLC1,192.0.2.39,502"),
        ("名前,機器IP,ポート", "PLC1,192.0.2.39,502"),
        ("名前,PLC IP,ポート", "PLC1,192.0.2.39,502"),
        ("Name,IP address (device),Port", "PLC1,192.0.2.39,502"),
        ("名前,説明,機器IP,ポート", "PLC1,プレス機,192.0.2.39,502"),
        ("名前,ゲートウェイIP,IPアドレス,ポート", "PLC1,192.0.2.1,192.0.2.39,502"),
        ("名前,機器IP,ゲートウェイIP,ポート", "PLC1,192.0.2.39,192.0.2.1,502"),
    ],
)
def test_a_host_column_named_for_its_device_is_the_host(header, row):
    assert _rows(parse_csv_text(f"{header}\n{row}\n")) == [
        ("PLC1", "192.0.2.39", [502])
    ]


@pytest.mark.parametrize(
    "header",
    [
        "名前,PLC IP,HMI IP",
        "名前,ゲートウェイIP",
        "名前,IP一覧",
        "名前,VIP",
        "名前,Equip",
    ],
)
def test_a_column_that_does_not_name_one_device_address_is_not_the_host(header):
    with pytest.raises(InventoryError, match="no IP address found"):
        parse_csv_text(f"{header}\nPLC1,192.0.2.39,192.0.2.40\n")


@pytest.mark.parametrize(
    "column", ["スイッチポート", "HUBポート", "LANポート", "Switch Port", "UDP Port"]
)
def test_a_switch_port_column_is_not_read_as_tcp_ports(tmp_path, capsys, column):
    path = _write(
        tmp_path,
        "l.csv",
        f"名前,IPアドレス,接続先スイッチ,{column},備考\n"
        "PLC1,192.0.2.39,SW-01,Gi1/0/12,盤1\n",
    )
    dev = load_devices(path)[0]
    assert (dev.host, dev.ports, dev.note) == ("192.0.2.39", [], "盤1")
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert f"column '{column}' is not read as TCP ports" in err


def test_the_switch_port_notice_is_in_the_users_language(ja, capsys):
    devs = parse_csv_text(
        "名前,IPアドレス,ポート,スイッチポート\nPLC1,192.0.2.39,502,12\n"
    )
    assert _rows(devs) == [("PLC1", "192.0.2.39", [502])]
    assert "列「スイッチポート」はTCPポートとして読みません" in capsys.readouterr().err


def test_columns_that_only_contain_the_letters_port_get_no_notice(capsys):
    parse_csv_text(
        "名前,IPアドレス,ポート,サポート窓口,Report\nPLC1,192.0.2.39,502,,\n"
    )
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("cell", ["502²", "①", "502.5", "0", "70000"])
def test_unreadable_ports_are_errors_with_the_line(cell):
    with pytest.raises(InventoryError, match="line 3: "):
        parse_csv_text(f"name,host,ports\nOK,192.0.2.1,502\nPLC,192.0.2.2,{cell}\n")


def test_an_ipv6_cell_with_brackets_keeps_its_port():
    dev = parse_csv_text("名前,IPアドレス,ポート\nPLC1,[2001:db8::1]:502,\n")[0]
    assert (dev.host, dev.ports) == ("2001:db8::1", [502])


def test_a_short_row_is_refused_instead_of_using_the_name_as_host():
    with pytest.raises(InventoryError, match="line 3: missing IP address/host"):
        parse_csv_text("名前,IPアドレス,ポート\nPLC1,192.0.2.39\nPLC2\n")


@pytest.mark.parametrize(
    "cell, mac",
    [
        ("00-26-92-AA-BB-CC", "00:26:92:aa:bb:cc"),
        ("0026.92aa.bbcc", "00:26:92:aa:bb:cc"),
        ("002692AABBCC", "00:26:92:aa:bb:cc"),
    ],
)
def test_macs_in_common_forms_are_normalised(cell, mac):
    assert parse_csv_text(f"name,host,mac\nGW,192.0.2.1,{cell}\n")[0].mac == mac


@pytest.mark.parametrize("cell", ["00:50:56:f7:a9", "0050.56f7", "zz:50:56:f7:a9:be"])
def test_a_mac_netdiag_cannot_read_is_refused(cell):
    with pytest.raises(InventoryError, match=f"line 2: not a MAC address: {cell}"):
        parse_csv_text(f"name,host,mac\nGW,192.0.2.1,{cell}\n")


def test_yaml_reads_values_as_written(tmp_path):
    path = _write(
        tmp_path,
        "line.yaml",
        "defaults:\n"
        "  ports: [502]\n"
        "  group: LineA\n"
        "devices:\n"
        "  - {name: PLC1, host: 192.0.2.39, port: 4840, ライン: LineB}\n"
        "  - {name: PLC2, host: 192.0.2.40, ports: 0502, mac: 12:34:56:01:23:45}\n"
        "  - 192.0.2.41\n",
        "utf-8",
    )
    devs = load_devices(path)
    assert [(d.name, d.ports, d.group, d.mac) for d in devs] == [
        ("PLC1", [4840], "LineB", None),
        ("PLC2", [502], "LineA", "12:34:56:01:23:45"),
        ("192.0.2.41", [502], "LineA", None),
    ]


def test_toml_device_keys_win_over_defaults(tmp_path):
    path = _write(
        tmp_path,
        "line.toml",
        '[defaults]\nports = [502]\n\n[[devices]]\nname = "PLC1"\nhost = "192.0.2.39"\n'
        'port = 4840\n\n[[devices]]\nname = "PLC2"\nhost = "192.0.2.40"\n',
        "utf-8",
    )
    assert [d.ports for d in load_devices(path)] == [[4840], [502]]


def test_a_scenario_mac_already_parsed_as_a_number_asks_for_quotes():
    with pytest.raises(InventoryError, match="quote it in YAML"):
        parse_structured([{"host": "192.0.2.1", "mac": 9783941025}])


def test_an_excel_workbook_gets_the_save_as_csv_hint(tmp_path):
    path = tmp_path / "台帳.xlsx"
    path.write_bytes(b"PK\x03\x04" + b"\0" * 64)
    with pytest.raises(InventoryError, match=r"save it as CSV"):
        load_devices(path)


def test_the_unused_interval_column_still_loads(tmp_path):
    path = _write(tmp_path, "l.csv", "名前,IPアドレス,監視間隔\nPLC1,192.0.2.39,1.5\n")
    dev = load_devices(path)[0]
    assert (dev.host, "interval" in dev.to_dict()) == ("192.0.2.39", False)


def test_ledger_errors_are_input_errors_in_the_users_language(ja):
    with pytest.raises(
        InputError, match="3行目: ポート番号またはプロトコル名として読めません"
    ):
        parse_csv_text("名前,IPアドレス,ポート\nA,192.0.2.1,502\nB,192.0.2.2,abc\n")


def test_check_with_a_bad_ledger_exits_4_naming_file_and_line(tmp_path, capsys):
    path = _write(
        tmp_path, "台帳.csv", "名前,IPアドレス,ポート\nPLC1,192.0.2.39,modbis\n"
    )
    with pytest.raises(SystemExit) as exc:
        main(["check", "-f", str(path)], prog_name="netdiag")
    assert exc.value.code == 4
    err = capsys.readouterr().err
    assert "台帳.csv: line 2: unknown port or protocol: 'modbis'" in err
