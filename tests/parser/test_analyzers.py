"""Representative BSL, query and metadata extraction tests."""

from open1c_analyzer.parser import BslAnalyzer, MetadataParser


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
