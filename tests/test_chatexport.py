from datetime import datetime, timezone

from warescue import chatexport

SAMPLE = """\ufeff2.10.2026 13:20 - Mesajlar ve aramalar uçtan uca şifrelidir.
2.10.2026 13:29 - Kişi 1: uygulama yapak
2.10.2026 13:31 - \u200eKişi 2 ⛮⛮⛮: ilk satır
ikinci satır: iki nokta da var
2.10.2026 13:32 - Kişi 2 ⛮⛮⛮: <Medya dahil edilmedi>
2.10.2026 13:33 - Kişi 1: \u200eIMG-20261002-WA0001.jpg (dosya ekli)
güzel foto
2.10.2026 13:34 - Kişi 1: Bu mesaj silindi
12.10.2026 9:05 - Kişi 2 ⛮⛮⛮: düzelttim <Bu mesaj düzenlendi>
"""


def parse():
    return chatexport.parse_lines(SAMPLE.splitlines(keepends=True))


def test_counts_and_kinds():
    msgs = parse()
    assert [m.kind for m in msgs] == ["system", "text", "text", "media_omitted", "media", "deleted", "text"]


def test_multiline_and_invisible_chars():
    msg = parse()[2]
    assert msg.sender == "Kişi 2 ⛮⛮⛮"
    assert msg.text == "ilk satır\nikinci satır: iki nokta da var"


def test_media_caption_and_edit():
    msgs = parse()
    assert msgs[4].media_file == "IMG-20261002-WA0001.jpg" and msgs[4].text == "güzel foto"
    assert msgs[6].edited and msgs[6].text == "düzelttim"


def test_timezone_conversion():
    assert parse()[1].time.astimezone(timezone.utc) == datetime(2026, 10, 2, 10, 29, tzinfo=timezone.utc)


def test_alternative_formats():
    ios = chatexport.parse_lines(["[2.10.2026 13:29:05] Ali: selam\n"])
    slash = chatexport.parse_lines(["2/10/26, 1:29 PM - Ali: selam\n"])
    assert ios[0].time.second == 5
    assert slash[0].time.hour == 13 and slash[0].time.day == 2


MORE = """2.10.2026 14:00 - A: <Video note omitted>
2.10.2026 14:01 - A: Ali.vcf (dosya ekli)
2.10.2026 14:02 - B: konum: https://maps.google.com/?q=39.9,32.8
2.10.2026 14:03 - B: ANKET:
Ne yiyelim?
SEÇENEK: Pizza (1 oy)
2.10.2026 14:04 - A: <!DOCTYPE html>
<html>
<div class="x">
</html>
2.10.2026 14:05 - A: <binmek>
"""


def test_extra_kinds_and_html_stays_text():
    msgs = chatexport.parse_lines(MORE.splitlines(keepends=True))
    assert [m.kind for m in msgs] == ["media_omitted", "contact", "location", "poll", "text", "text"]
    assert msgs[0].media_label == "Video note"
    assert msgs[4].text.count("\n") == 3
