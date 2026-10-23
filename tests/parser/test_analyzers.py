"""Representative BSL, query and metadata extraction tests."""

import pytest

from open1c_analyzer.parser import BslAnalyzer, MetadataParser


def test_call_ownership_preserves_gaps_and_same_line_declarations() -> None:
    parsed = BslAnalyzer().parse(
        'Сообщить("module");\n'
        'Процедура Первая()\nСообщить("first");\nКонецПроцедуры\n'
        'Сообщить("gap");\n'
        'Процедура Вторая() Сообщить("second"); КонецПроцедуры '
        'Процедура Третья() Сообщить("third"); КонецПроцедуры\n'
        'Сообщить("after");'
    )
    assert [item.name for item in parsed.symbols] == ["Первая", "Вторая", "Третья"]
    assert [call.caller_ordinal for call in parsed.calls] == [None, 0, None, 1, 1, None]


def test_bsl_analyzer_extracts_core_facts() -> None:
    text = """#Область ПубличныйИнтерфейс
&НаСервере
Функция ПолучитьОстаток(Склад, Знач Номенклатура = Неопределено) Экспорт
    Запрос = Новый Запрос;
    Запрос.Текст =
    "ВЫБРАТЬ
    | Остатки.КоличествоОстаток
    |ИЗ
    | РегистрНакопления.ОстаткиТоваров.Остатки(&Дата, Склад = &Склад) КАК Остатки";
    Возврат Запрос.Выполнить();
КонецФункции
#КонецОбласти
Процедура Провести()
    ПолучитьОстаток(Склад);
    Документы.ЗаказПокупателя.НайтиПоНомеру("1");
КонецПроцедуры
"""
    parsed = BslAnalyzer().parse(text)
    assert [item.name for item in parsed.symbols] == ["ПолучитьОстаток", "Провести"]
    assert parsed.symbols[0].is_export is True
    assert parsed.symbols[0].directive == "НаСервере"
    assert parsed.symbols[0].region == "ПубличныйИнтерфейс"
    assert [item.name for item in parsed.symbols[0].parameters] == ["Склад", "Номенклатура"]
    assert parsed.symbols[0].parameters[1].by_value is True
    assert parsed.symbols[0].parameters[1].default == "Неопределено"
    assert any(item.full_name == "ПолучитьОстаток" for item in parsed.calls)
    assert not any(item.full_name == "ПолучитьОстаток" and item.line == 3 for item in parsed.calls)
    assert parsed.queries[0].tables[0].name == "РегистрНакопления.ОстаткиТоваров.Остатки"
    assert any(item.target_full_name == "Документ.ЗаказПокупателя" for item in parsed.references)


def test_metadata_parser_extracts_children_references_and_profile() -> None:
    text = """<MetaDataObject xmlns:cfg="urn:test"><Catalog uuid="cat-1"><Properties>
    <Name>Номенклатура</Name><CompatibilityMode>Version8_3_17</CompatibilityMode>
    <Synonym><item><content>Номенклатура</content></item></Synonym></Properties>
    <ChildObjects><Attribute uuid="attr-1"><Properties><Name>ОсновнойСклад</Name>
    <Type><Type>cfg:CatalogRef.Склады</Type></Type></Properties></Attribute></ChildObjects>
    </Catalog></MetaDataObject>"""
    parsed = MetadataParser().parse(text, "Catalogs/Номенклатура.xml")
    assert [(item.kind, item.name) for item in parsed.objects] == [
        ("catalog", "Номенклатура"),
        ("attribute", "ОсновнойСклад"),
    ]
    assert parsed.references[0].target_full_name == "Справочник.Склады"
    assert parsed.profile["CompatibilityMode"] == "Version8_3_17"


def test_metadata_parser_recognizes_functional_options() -> None:
    text = """<MetaDataObject><FunctionalOption><Properties>
    <Name>АвтономнаяРабота</Name><Synonym><item><content>Автономная работа</content>
    </item></Synonym></Properties></FunctionalOption></MetaDataObject>"""

    parsed = MetadataParser().parse(
        text,
        "FunctionalOptions/АвтономнаяРабота.xml",
    )

    assert parsed.objects[0].kind == "functional_option"
    assert parsed.objects[0].full_name == "ФункциональнаяОпция.АвтономнаяРабота"


def test_metadata_parser_keeps_unknown_object_kinds_distinct() -> None:
    first = MetadataParser().parse(
        "<MetaDataObject><FirstKind><Properties><Name>ОбщийОбъект</Name>"
        "</Properties></FirstKind></MetaDataObject>",
        "FirstKinds/ОбщийОбъект.xml",
    )
    second = MetadataParser().parse(
        "<MetaDataObject><SecondKind><Properties><Name>ОбщийОбъект</Name>"
        "</Properties></SecondKind></MetaDataObject>",
        "SecondKinds/ОбщийОбъект.xml",
    )

    assert first.objects[0].full_name == "firstkind.ОбщийОбъект"
    assert second.objects[0].full_name == "secondkind.ОбщийОбъект"


def test_bsl_analyzer_does_not_treat_language_keywords_as_calls() -> None:
    text = """Процедура Проверить()
    Если Истина И (Ложь ИЛИ Не (Истина)) Тогда
        Возврат;
    КонецЕсли;
КонецПроцедуры
"""

    parsed = BslAnalyzer().parse(text)

    assert [item.full_name for item in parsed.calls] == []


def test_bsl_analyzer_does_not_treat_extension_annotations_as_calls() -> None:
    text = """&ИзменениеИКонтроль("Проверить")
Процедура МКО_Проверить()
КонецПроцедуры

&Вместо("Записать")
Процедура МКО_Записать()
КонецПроцедуры

&Перед("Провести")
Процедура МКО_ПередПроведением()
КонецПроцедуры

&После("Провести")
Процедура МКО_ПослеПроведения()
КонецПроцедуры
"""

    parsed = BslAnalyzer().parse(text)

    assert [item.full_name for item in parsed.calls] == []


def test_metadata_parser_recognizes_nested_managed_form_identity() -> None:
    text = """<Form xmlns:v8=\"urn:test\"><Events/><ChildItems/></Form>"""

    parsed = MetadataParser().parse(
        text,
        "Documents/РасходнаяНакладная/Forms/ФормаСписка/Ext/Form.xml",
    )

    assert parsed.objects[0].kind == "form"
    assert parsed.objects[0].name == "ФормаСписка"
    assert parsed.objects[0].full_name == "Документ.РасходнаяНакладная.Form.ФормаСписка"
    assert parsed.objects[0].parent_full_name == "Документ.РасходнаяНакладная"


def test_bsl_analyzer_extracts_register_write_surfaces() -> None:
    text = """Процедура Провести()
    ПроведениеДокументовУНФ.ОтразитьДвижения(
        "СуммыДокументовРегламентированныйУчет", ТаблицыДляДвижений, Движения, Отказ);
    Движения.Запасы.Записывать = Истина;
    Набор = РегистрыСведений.Контроль. СоздатьНаборЗаписей();
    Набор.Записать();
КонецПроцедуры
""".replace(". Создать", ".Создать")

    parsed = BslAnalyzer().parse(text)
    writes = [item for item in parsed.references if item.relation == "register_write"]

    assert any(item.target_name == "СуммыДокументовРегламентированныйУчет" for item in writes)
    assert any(item.target_name == "Запасы" for item in writes)
    assert any(
        item.target_full_name == "РегистрСведений.Контроль"
        and item.target_kind == "information_register"
        for item in writes
    )


@pytest.mark.parametrize(
    "statement",
    [
        "Количество = Движения.Запасы.Количество();",
        "Строки = Движения.Запасы.Выгрузить();",
        "Сообщить(Движения.Запасы.Записывать);",
        "Движения.Запасы.Записывать = Ложь;",
        "Если Движения.Запасы.Записывать = Истина Тогда Сообщить(1); КонецЕсли;",
        "Объект.Движения.Запасы.Добавить();",
    ],
)
def test_movement_reads_and_disabled_writes_are_not_writers(statement: str) -> None:
    parsed = BslAnalyzer().parse(f"Процедура Проверка()\n{statement}\nКонецПроцедуры")
    assert not [item for item in parsed.references if item.relation == "register_write"]


@pytest.mark.parametrize(
    "statement",
    [
        "Движения.Запасы.Добавить();",
        "Движения.Запасы.Очистить();",
        "Движения.Запасы.Записать();",
        "Движения.Запасы.Записывать = Истина;",
        "Movements.Запасы.Write = True;",
    ],
)
def test_explicit_movement_mutations_remain_writers(statement: str) -> None:
    parsed = BslAnalyzer().parse(f"Процедура Провести()\n{statement}\nКонецПроцедуры")
    assert {
        item.target_name for item in parsed.references if item.relation == "register_write"
    } == {"Запасы"}


@pytest.mark.parametrize(
    "tail,expected",
    [
        ("Набор = РегистрыСведений.B.СоздатьНаборЗаписей(); Набор.Записать();", {"B"}),
        ("Набор = Неопределено; Набор.Записать();", set()),
        (
            "Набор.Записать(); Набор = РегистрыСведений.B.СоздатьНаборЗаписей(); Набор.Записать();",
            {"A", "B"},
        ),
        ("Если\nНабор = Неопределено Тогда Сообщить(1); КонецЕсли; Набор.Записать();", {"A"}),
        ("Объект.Набор = Неопределено; Набор.Записать();", {"A"}),
        ("КонецПроцедуры\nПроцедура Другая()\nНабор.Записать();", set()),
    ],
)
def test_record_set_binding_ends_on_reassignment_and_is_procedure_local(
    tail: str, expected: set[str]
) -> None:
    source = (
        "Процедура Проверка(\nПараметр\n) Экспорт\n"
        "Набор = РегистрыСведений.A.\nСоздатьНаборЗаписей();\n"
        f"{tail}\nКонецПроцедуры"
    )
    parsed = BslAnalyzer().parse(source)
    assert {
        item.target_name for item in parsed.references if item.relation == "register_write"
    } == expected


def test_converted_record_set_is_not_bound_as_a_record_set() -> None:
    parsed = BslAnalyzer().parse(
        "Процедура Проверка()\n"
        "Набор = РегистрыСведений.A.СоздатьНаборЗаписей().Выгрузить();\n"
        "Набор.Записать();\nКонецПроцедуры"
    )
    assert not [item for item in parsed.references if item.relation == "register_write"]


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Движения.Запасы.Записывать = Истина;", {"Запасы"}),
        ("Набор = РегистрыСведений.A.СоздатьНаборЗаписей(); Набор.Записать();", {"A"}),
        (
            "Набор = РегистрыСведений.A.СоздатьНаборЗаписей();\n"
            "#Область Другая\nНабор = РегистрыСведений.B.СоздатьНаборЗаписей();\n"
            "#КонецОбласти\nНабор.Записать();",
            {"B"},
        ),
    ],
)
def test_register_assignment_boundaries_preserve_preprocessor_directives(
    body: str, expected: set[str]
) -> None:
    parsed = BslAnalyzer().parse(
        f"Процедура Проверка()\n#Область Запись\n{body}\n#КонецОбласти\nКонецПроцедуры"
    )
    assert {
        item.target_name for item in parsed.references if item.relation == "register_write"
    } == expected
