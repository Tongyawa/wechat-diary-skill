import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from wechat_diary_core.backends.weflow_api.appmsg import AppmsgMeta, _sanitize_xml, parse_appmsg
from wechat_diary_core.backends.weflow_api.mapper import map_session_json


# FILE/JOIN/PAT/TRANSFER 保留真机 XML 骨架，所有可识别文本均已换成中性占位。
# 5/19/33/51/4/24/1/36/3/63/50/87/2001 没有独立真机骨架：这些 fixture
# 以真机 FILE 骨架为模板，仅替换内部 <type> 与中性 title/des，且始终与外部 subtype 对齐。
FILE_XML = '''<?xml version="1.0"?>
<msg>
	<appmsg appid="app-placeholder" sdkver="0">
		<title>示例报告.docx</title>
		<des />
		<action />
		<type>6</type>
		<showtype>0</showtype>
		<appattach>
			<totallen>88770</totallen>
			<attachid>attachment-placeholder</attachid>
			<emoticonmd5 />
			<fileext>docx</fileext>
			<cdnattachurl>cdn-placeholder</cdnattachurl>
		</appattach>
		<md5>md5-placeholder</md5>
	</appmsg>
</msg>'''

JOIN_XML = '''<?xml version="1.0"?>
<msg>
	<appmsg>
		<title>#接龙

8.7

1. 示例事项
2. 成员甲-7.19-9.7
3. 成员乙-7.19-9.5</title>
		<action>view</action>
		<type>53</type>
	</appmsg>
</msg>'''

PAT_XML = '''<msg><appmsg appid="" sdkver="0"><title>"成员甲" 拍了拍 "成员乙" [烟花]</title><des></des><action></action><type>62</type><showtype>0</showtype><appattach><totallen>0</totallen><attachid></attachid><emoticonmd5></emoticonmd5><fileext>" 拍了拍 "成员乙" [烟花]</fileext><aeskey></aeskey></appattach><extinfo></extinfo></appmsg></msg>'''

TRANSFER_XML = '''<msg>
<appmsg appid="" sdkver="">
<title><![CDATA[微信转账]]></title>
<des><![CDATA[收到转账58.75元。如需收钱，请点此升级至最新版本]]></des>
<action></action>
<type>2000</type>
<content><![CDATA[]]></content>
</appmsg>
</msg>'''

REPLY_TEXT = "回复正文占位"
RAW_WITH_PREFIX = "wxid_sender_placeholder:\n" + JOIN_XML


def _templated_xml(app_type: int, title: str, des: str = "") -> str:
    return (
        FILE_XML.replace("<type>6</type>", f"<type>{app_type}</type>")
        .replace("<title>示例报告.docx</title>", f"<title>{title}</title>")
        .replace("<des />", f"<des>{des}</des>")
    )


def _message(local_type, content, *, raw_content=None, local_id=1, **extra):
    message = {
        "localId": local_id,
        "createTime": local_id,
        "localType": local_type,
        "content": content,
        "senderUsername": "wxid_sender_placeholder",
        "isSend": 0,
    }
    if raw_content is not None:
        message["rawContent"] = raw_content
    message.update(extra)
    return message


def _mapped(message, *, max_chars=300):
    data = map_session_json(
        {"username": "wxid_contact_placeholder", "displayName": "联系人占位"},
        [message],
        contacts=[],
        appmsg_text_max_chars=max_chars,
    )
    return data["messages"][0]


class AppmsgParserTests(unittest.TestCase):
    def test_parser_recovers_metadata_from_trailing_nuls(self):
        xml = ('<?xml version="1.0"?><msg><appmsg><title>Report A.txt</title>'
               '<des>Summary A</des><type>6</type><appattach><fileext>txt</fileext>'
               '<totallen>2048</totallen></appattach></appmsg></msg>\x00\x00')
        self.assertEqual(parse_appmsg(xml), AppmsgMeta("Report A.txt", "Summary A", "txt", 2048))
        self.assertEqual(_mapped(_message((6 << 32) | 49, "[文件]", raw_content=xml))["content"],
                         "[文件：Report A.txt（2.0 KB）]")

    def test_parser_recovers_nested_declaration_after_group_prefix(self):
        xml = ('<msg><?xml version="1.0"?><appmsg><title>Report B.txt</title>'
               '<des>Summary B</des><type>6</type><appattach><fileext>txt</fileext>'
               '<totallen>3072</totallen></appattach></appmsg></msg>')
        self.assertEqual(parse_appmsg(xml), AppmsgMeta("Report B.txt", "Summary B", "txt", 3072))
        message = _message((6 << 32) | 49, "[文件]", raw_content="wxid_sender_placeholder:\n" + xml)
        mapped = map_session_json({"username": "group_placeholder@chatroom"}, [message], contacts=[])["messages"][0]
        self.assertEqual(mapped["content"], "[文件：Report B.txt（3.0 KB）]")

    def test_parser_recovers_illegal_numeric_character_references(self):
        xml = ('<msg><appmsg><title>Report&#20; C.txt</title><des>Summary&#x0B; C</des>'
               '<type>6</type><appattach><fileext>t&#x1F;xt</fileext>'
               '<totallen>40&#0;96</totallen></appattach></appmsg></msg>')
        self.assertEqual(parse_appmsg(xml), AppmsgMeta("Report C.txt", "Summary C", "txt", 4096))
        self.assertEqual(_mapped(_message((6 << 32) | 49, "[文件]", raw_content=xml))["content"],
                         "[文件：Report C.txt（4.0 KB）]")

    def test_sanitization_preserves_legal_xml_and_original_parse_results(self):
        legal = [FILE_XML, JOIN_XML, PAT_XML, TRANSFER_XML,
                 '\ufeff' + FILE_XML,
                 ('<?xml version="1.0"?><?xml-stylesheet href="literal&#20;.xsl"?>'
                  '<msg><!-- literal &#20; <?xml version="1.0"?> -->'
                  '<appmsg><title><![CDATA[Literal &#20; <?xml version="1.0"?>]]></title>'
                  '<des>&amp;#20; &lt;?xml version="1.0"?&gt; &amp; &quot; &apos;</des>'
                  '<appattach><fileext>txt</fileext><totallen>17</totallen></appattach></appmsg></msg>'),
                 ('<?note literal &#20;?><appmsg attr="&#9;&#10;&#13;">'
                  '<title>Edges &#32;&#xD7FF;&#xE000;&#xFFFD;&#x10000;&#x10FFFF;</title>'
                  '<des>Keep \t LF\n CR\r and emoji 🌿</des></appmsg>')]
        for case in json.loads((Path(__file__).parent / "fixtures" / "weflow_media_appmsg.json").read_text(encoding="utf-8"))["cases"]:
            xml = case["rawContent"].removeprefix("wxid_sender_placeholder:\n")
            legal.append(xml)
        for xml in legal:
            with self.subTest(case=legal.index(xml)):
                old_tree = ET.fromstring(xml)  # independent proof the input is legal
                clean = _sanitize_xml(xml)
                self.assertEqual(xml, clean)
                self.assertEqual(ET.tostring(old_tree), ET.tostring(ET.fromstring(clean)))
                self.assertIsNotNone(parse_appmsg(xml))
                self.assertEqual(parse_appmsg(xml), parse_appmsg(clean))

    def test_recovery_preserves_cdata_comments_and_ordinary_instructions(self):
        xml = ('<?xml version="1.0"?><?xml-stylesheet href="literal&#20;.xsl"?>'
               '<msg><!-- literal &#20; <?xml version="1.0"?> -->'
               '<appmsg><title><![CDATA[Literal &#20; <?xml version="1.0"?>]]></title>'
               '<des>Keep &amp;#20; exactly</des><appattach><fileext>txt</fileext>'
               '<totallen>17</totallen></appattach></appmsg></msg>')
        expected = parse_appmsg(xml)
        self.assertEqual(expected.title, 'Literal &#20; <?xml version="1.0"?>')
        self.assertEqual(expected.des, "Keep &#20; exactly")
        # Force the recovery path; broad reference/declaration regexes would
        # silently damage the legal CDATA and PI text while making parsing pass.
        self.assertEqual(_sanitize_xml(xml + '\x00'), xml)
        self.assertEqual(parse_appmsg(xml + '\x00'), expected)

    def test_recovery_removes_only_forbidden_literal_and_reference_codepoints(self):
        legal = [9, 10, 13, 0x20, 0xD7FF, 0xE000, 0xFFFD, 0x10000, 0x10FFFF]
        illegal = [0, 1, 8, 11, 12, 14, 20, 31, 0xD800, 0xDFFF, 0xFFFE, 0xFFFF, 0x110000]
        for value in legal + illegal:
            for reference in (f"&#{value};", f"&#x{value:X};"):
                xml = f'<appmsg><title>Before{reference}After</title></appmsg>'
                with self.subTest(value=value, hex=reference.startswith('&#x')):
                    if value in legal:
                        self.assertEqual(_sanitize_xml(xml), xml)
                    else:
                        self.assertEqual(_sanitize_xml(xml), '<appmsg><title>BeforeAfter</title></appmsg>')
                        self.assertEqual(parse_appmsg(xml).title, "BeforeAfter")
            if value <= 0x10FFFF:
                xml = f'<appmsg><title>Before{chr(value)}After</title></appmsg>'
                self.assertEqual(_sanitize_xml(xml), xml if value in legal else '<appmsg><title>BeforeAfter</title></appmsg>')
        # Huge numbers are invalid, but arbitrarily many leading zeroes do not
        # turn a legal reference into an illegal one.
        huge = '&#' + '9' * 5000 + ';'
        zero_padded = '&#' + '0' * 5000 + '65;'
        self.assertEqual(parse_appmsg(f'<appmsg><title>A{huge}B</title></appmsg>').title, 'AB')
        self.assertEqual(_sanitize_xml(f'<appmsg><title>{zero_padded}</title></appmsg>'),
                         f'<appmsg><title>{zero_padded}</title></appmsg>')

    def test_recovery_does_not_guess_other_broken_markup_or_declared_text_in_attributes(self):
        for xml in ('<appmsg><title>Unclosed', '<appmsg><title>A & B</title></appmsg>',
                    '<appmsg><title>&unknown;</title></appmsg>',
                    '<appmsg attr=\'<?xml version="1.0"?>\'><title>Keep</title></appmsg>'):
            with self.subTest(case=xml):
                self.assertIsNone(parse_appmsg(xml))
                self.assertEqual(_sanitize_xml(xml), xml)

    def test_parser_reads_file_metadata_and_normalizes_text(self):
        meta = parse_appmsg(FILE_XML)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.title, "示例报告.docx")
        self.assertEqual(meta.des, "")
        self.assertEqual(meta.fileext, "docx")
        self.assertEqual(meta.totallen, 88770)

    def test_parser_normalizes_join_title_and_cdata(self):
        self.assertEqual(
            parse_appmsg(JOIN_XML).title,
            "#接龙 / 8.7 / 1. 示例事项 / 2. 成员甲-7.19-9.7 / 3. 成员乙-7.19-9.5",
        )
        meta = parse_appmsg(TRANSFER_XML)
        self.assertEqual(meta.title, "微信转账")
        self.assertEqual(meta.des, "收到转账58.75元。如需收钱，请点此升级至最新版本")

    def test_boilerplate_match_is_exact_and_preserves_real_text_containing_it(self):
        title = "转发说明：当前版本不支持展示该内容，请升级至最新版本。"
        self.assertEqual(parse_appmsg(_templated_xml(51, title)).title, title)

    def test_malformed_non_appmsg_and_unpaired_surrogate_return_none(self):
        for content in ["", "[视频]", "<msg><appmsg><title>未闭合", "<msg></msg>", "\ud800"]:
            with self.subTest(content=repr(content)):
                self.assertIsNone(parse_appmsg(content))

    def test_title_truncation_is_configurable_and_clamps_direct_zero_to_one(self):
        long_xml = _templated_xml(53, "标题" * 20)
        self.assertEqual(parse_appmsg(long_xml, max_chars=5).title, "标题标题标…")
        self.assertEqual(parse_appmsg(long_xml, max_chars=0).title, "标…")
        message = _message((53 << 32) | 49, long_xml)
        self.assertTrue(_mapped(message, max_chars=5)["content"].endswith("…"))
        self.assertNotEqual(_mapped(message, max_chars=5)["content"], _mapped(message)["content"])


class AppmsgMappingTests(unittest.TestCase):
    def test_paired_media_payloads_restore_metadata_and_keep_reply_fields(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "weflow_media_appmsg.json").read_text(encoding="utf-8"))
        for case in fixture["cases"]:
            with self.subTest(app=case["app"]):
                raw = case["rawContent"]
                xml = raw.split(":\n", 1)[1] if raw.startswith("wxid_sender_placeholder:\n") else raw
                local_type = (case["app"] << 32) | 49
                outputs = []
                for content in (xml, case["content"]):
                    message = _message(local_type, content, raw_content=raw, parsedContent=case["content"])
                    session = "group_placeholder@chatroom" if case.get("group") else "wxid_contact_placeholder"
                    mapped = map_session_json({"username": session, "displayName": "Contact placeholder"}, [message], contacts=[])["messages"][0]
                    self.assertEqual(mapped["content"], case["expected"])
                    self.assertNotIn("rawContent", mapped)
                    outputs.append(mapped["content"])
                self.assertEqual(*outputs)
        # These guards are already green before the fix: raw XML must not replace reply text.
        for group in (False, True):
            for mode in (0, 1):
                raw = ("wxid_sender_placeholder:\n" if group else "") + '<msg><appmsg><title>Raw title differs</title><type>57</type><refermsg/></appmsg></msg>'
                message = _message((57 << 32) | 49, "Reply body", raw_content=raw, parsedContent="Reply body" if mode else "", quote={"content": "Quoted body", "sender": "wxid_sender_placeholder"})
                mapped = _mapped(message)
                self.assertEqual(mapped["content"], "Reply body")
                self.assertEqual(mapped["quotedContent"], "Quoted body")
        xml = '<msg><appmsg><title>Raw title</title><type>5</type></appmsg></msg>'
        other_xml = xml.replace("Raw title", "Content title")
        self.assertEqual(_mapped(_message((5 << 32) | 49, other_xml, raw_content=xml))["content"], "[链接] Raw title")
        for invalid_raw in (None, "", "<broken>", "[链接]", "arbitrary text:\n" + xml, "\ud800"):
            with self.subTest(invalid_raw=repr(invalid_raw)):
                self.assertEqual(_mapped(_message((5 << 32) | 49, other_xml, raw_content=invalid_raw))["content"], "[链接] Content title")

    def test_media_location_and_recall_preserve_their_readable_content(self):
        xml = '<msg><location x="0" y="0" label="Street placeholder" poiname="Place placeholder"/></msg>'
        for prefix in ("", "wxid_sender_placeholder:\n"):
            for content in ("[位置]", "[位置] Street placeholder"):
                with self.subTest(prefix=bool(prefix), content=content):
                    self.assertEqual(_mapped(_message(48, content, raw_content=prefix + xml))["content"], "[位置] Street placeholder")
        for content in ('<msg><location label="Address only"/></msg>', '<msg><location label="" poiname="Address only"/></msg>', '<msg><location label="  " poiname="Address only"/></msg>'):
            self.assertEqual(_mapped(_message(48, content))["content"], "[位置] Address only")
        self.assertEqual(_mapped(_message(48, "[位置] Rendered address", raw_content="<broken>"))["content"], "[位置] Rendered address")
        self.assertEqual(_mapped(_message(48, "unrecognized text", raw_content="<broken>"))["content"], "[位置]")
        # Already green on baseline; system/text content takes precedence over raw XML.
        for base, content in ((10000, '"Sender placeholder" 撤回了一条消息'), (1, "Plain text")):
            mapped = _mapped(_message(base, content, raw_content='<sysmsg type="revokemsg"><revokemsg/></sysmsg>'))
            self.assertEqual(mapped["content"], content)

    def test_real_skeleton_rendering_fixtures(self):
        cases = [
            (6, FILE_XML, "[文件：示例报告.docx（86.7 KB）]"),
            (53, JOIN_XML, "[接龙] #接龙 / 8.7 / 1. 示例事项 / 2. 成员甲-7.19-9.7 / 3. 成员乙-7.19-9.5"),
            (62, PAT_XML, '"成员甲" 拍了拍 "成员乙" [烟花]'),
            (2000, TRANSFER_XML, "[转账] 收到转账58.75元。如需收钱，请点此升级至最新版本"),
        ]
        for app_type, xml, expected in cases:
            with self.subTest(app_type=app_type):
                self.assertEqual(_mapped(_message((app_type << 32) | 49, xml))["content"], expected)
        self.assertEqual(_mapped(_message((57 << 32) | 49, REPLY_TEXT))["content"], REPLY_TEXT)

    def test_every_templated_render_branch_has_aligned_type_and_direct_assertion(self):
        cases = [
            (8, "忽略标题", "忽略描述", "[其他消息]"),
            (47, "忽略标题", "忽略描述", "[其他消息]"),
            (63, "视频标题", "", "[视频号] 视频标题"),
            (5, "链接标题", "链接摘要", "[链接] 链接标题：链接摘要"),
            (51, "动态标题", "", "[动态] 动态标题"),
            (33, "小程序标题", "", "[小程序] 小程序标题"),
            (19, "转发标题", "转发摘要", "[合并转发] 转发标题：转发摘要"),
            (4, "视频标题", "视频摘要", "[视频分享] 视频标题：视频摘要"),
            (2001, "红包标题", "红包摘要", "[红包] 红包摘要"),
            (87, "公告标题", "公告摘要", "[群公告]"),
            (24, "收藏标题", "收藏摘要", "[收藏] 收藏摘要"),
            (1, "链接标题", "链接摘要", "[链接] 链接标题"),
            (36, "分享标题", "分享摘要", "[分享] 分享标题"),
            (3, "音乐标题", "音乐作者", "[音乐] 音乐标题 - 音乐作者"),
            (50, "视频标题", "", "[视频号] 视频标题"),
            (74, "归档文件.zip", "", "[文件：归档文件.zip（86.7 KB）]"),
        ]
        for app_type, title, des, expected in cases:
            with self.subTest(app_type=app_type):
                xml = _templated_xml(app_type, title, des)
                self.assertIn(f"<type>{app_type}</type>", xml)
                mapped = _mapped(_message((app_type << 32) | 49, xml))
                self.assertEqual(mapped["content"], expected)

    def test_unsupported_version_boilerplate_falls_back_to_bare_labels(self):
        cases = [
            (51, "当前微信版本不支持展示该内容，请升级至最新版本。", "[动态]"),
            (63, "当前版本不支持展示该内容，请升级至最新版本", "[视频号]"),
            (50, "你的微信版本较低，不能接收外部红包，请升级微信", "[视频号]"),
        ]
        for app_type, title, expected in cases:
            with self.subTest(app_type=app_type):
                xml = _templated_xml(app_type, title)
                self.assertEqual(_mapped(_message((app_type << 32) | 49, xml))["content"], expected)

    def test_group_prefixed_raw_and_xml_content_render_the_same_join(self):
        mapped = _mapped(_message((53 << 32) | 49, JOIN_XML, raw_content=RAW_WITH_PREFIX))
        self.assertEqual(
            mapped["content"],
            "[接龙] #接龙 / 8.7 / 1. 示例事项 / 2. 成员甲-7.19-9.7 / 3. 成员乙-7.19-9.5",
        )

    def test_file_without_size_empty_title_and_both_path_separators(self):
        no_size = FILE_XML.replace("\t\t\t<totallen>88770</totallen>\n", "")
        self.assertEqual(_mapped(_message((6 << 32) | 49, no_size))["content"], "[文件：示例报告.docx]")
        empty_title = _templated_xml(74, "")
        self.assertEqual(_mapped(_message((74 << 32) | 49, empty_title))["content"], "[文件]")
        for title in ("/app/data/子目录/报告.pdf", r"C:\data\子目录\报告.pdf"):
            with self.subTest(title=title):
                xml = _templated_xml(6, title)
                self.assertEqual(_mapped(_message((6 << 32) | 49, xml))["content"], "[文件：报告.pdf（86.7 KB）]")

    def test_invalid_file_sizes_degrade_only_the_message(self):
        for total in ("-1", "1" + "0" * 400):
            with self.subTest(total_length=len(total)):
                xml = FILE_XML.replace("<totallen>88770</totallen>", f"<totallen>{total}</totallen>")
                self.assertEqual(_mapped(_message((6 << 32) | 49, xml))["content"], "[文件：示例报告.docx]")

    def test_pat_keeps_canonical_type_and_ignores_polluted_fileext(self):
        mapped = _mapped(_message((62 << 32) | 49, PAT_XML))
        self.assertEqual(mapped["type"], "其他消息")
        self.assertIn(" 拍了拍 ", mapped["content"])

    def test_reply_keeps_type_and_quote_fields(self):
        mapped = _mapped(
            _message(
                (57 << 32) | 49,
                REPLY_TEXT,
                replyToMessageId="server-quote-placeholder",
                quote={
                    "sender": "wxid_sender_placeholder",
                    "content": "被引用原文占位",
                    "accountName": "引用者占位",
                },
            )
        )
        self.assertEqual(mapped["type"], "引用消息")
        self.assertEqual(mapped["quotedContent"], "被引用原文占位")
        self.assertEqual(mapped["quotedSender"], "wxid_sender_placeholder")
        self.assertEqual(mapped["replyToMessageId"], "server-quote-placeholder")

    def test_base_media_placeholders_are_preserved(self):
        for base, expected, expected_type in [
            (42, "[名片]", "其他消息"),
            (43, "[视频]", "视频消息"),
            (48, "[位置]", "其他消息"),
        ]:
            with self.subTest(base=base):
                mapped = _mapped(_message(base, "[其他消息]"))
                self.assertEqual(mapped["type"], expected_type)
                self.assertEqual(mapped["content"], expected)

    def test_unknown_appmsg_uses_aligned_title_or_placeholder(self):
        with_title_xml = _templated_xml(999, "未知标题")
        no_title_xml = _templated_xml(999, "")
        with_title = _mapped(_message((999 << 32) | 49, with_title_xml))
        no_title = _mapped(_message((999 << 32) | 49, no_title_xml))
        self.assertEqual(with_title["content"], "[其他消息] 未知标题")
        self.assertEqual(no_title["content"], "[其他消息]")


if __name__ == "__main__":
    unittest.main()
