# -*- coding: utf-8 -*-
"""Приёмка хука: проверка первых секунд ролика ДО того, как куплены картинки.

Зачем. Замер 25.09: половина зрителей уходит в первые 30 секунд. Разбор 53 конкурентов
и наших 8 роликов показал, чем мы от них отличаемся:
  · мы начинаем с приставки места («В Нортбридж Текнолоджис…») — 6 роликов из 8,
    у конкурентов так начинают 4 из 53;
  · прямой речи в первую минуту у нас нет вовсе, у конкурентов в 16 из 53;
  · число или срок в первую минуту у конкурентов в 44 из 53;
  · обещание «что вы увидите» — у конкурентов в 49 из 53, у нас в 1 из 8;
  · картинок в первые 30 секунд у нас 6, у конкурентов 9–14.

Всё это считается по тексту и раскадровке, то есть ДО покупки картинок. Проверка ничего
не блокирует — она пишет отчёт и громко называет провалы в журнале.

Запуск отдельно: python3 hookqa.py <папка проекта>
"""
from __future__ import annotations

import json
import os
import re
import sys

# Приставки места: с них хук начинаться не должен.
МЕСТО = {
    "Russian": ("в", "во", "на", "у", "внутри", "посреди", "под", "за", "около", "возле"),
    "Polish": ("w", "we", "na", "u", "wewnątrz", "pośrodku", "pod", "za", "obok", "przy"),
    "Czech": ("v", "ve", "na", "u", "uvnitř", "uprostřed", "pod", "za", "vedle", "při"),
    "Portuguese": ("na", "no", "em", "dentro", "sob", "ao", "à", "nos", "nas", "perto"),
    "Spanish": ("en", "dentro", "sobre", "bajo", "junto", "cerca", "ante", "tras"),
    "Italian": ("nel", "nella", "in", "dentro", "sotto", "presso", "accanto", "al", "alla"),
    "Romanian": ("în", "la", "pe", "sub", "înăuntru", "lângă", "aproape"),
    "English": ("in", "at", "on", "inside", "within", "beneath", "under", "near", "beside"),
}

# Числа и сроки словами.
ЧИСЛА = {
    "Russian": r"\b(од(ин|на|ного)|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|"
               r"одиннадцать|двенадцать|двадцать|тридцать|сорок|пятьдесят|сто|тысяч\w*|"
               r"первый|второй|третий|последн\w+)\b",
    "Polish": r"\b(jed(en|na|nego)|dwa|dwie|trzy|cztery|pięć|sześć|siedem|osiem|dziewięć|dziesięć|"
              r"jedenaście|dwanaście|dwadzieścia|trzydzieści|czterdzieści|pięćdziesiąt|sto|tysi\w+|"
              r"pierwsz\w+|drug\w+|trzec\w+|ostatn\w+)\b",
    "Czech": r"\b(jed(en|na|noho)|dva|dvě|tři|čtyři|pět|šest|sedm|osm|devět|deset|jedenáct|dvanáct|"
             r"dvacet|třicet|čtyřicet|padesát|sto|tisíc\w*|prvn\w+|druh\w+|třet\w+|posledn\w+)\b",
    "Portuguese": r"\b(um|uma|dois|duas|três|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|vinte|"
                  r"trinta|quarenta|cinquenta|cem|mil|primeir\w+|segund\w+|terceir\w+|últim\w+)\b",
    "Spanish": r"\b(un|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|veinte|treinta|"
               r"cuarenta|cincuenta|cien|mil|primer\w*|segund\w+|tercer\w*|últim\w+)\b",
    "Italian": r"\b(un|una|due|tre|quattro|cinque|sei|sette|otto|nove|dieci|undici|dodici|venti|trenta|"
               r"quaranta|cinquanta|cento|mille|prim\w+|second\w+|terz\w+|ultim\w+)\b",
    "Romanian": r"\b(un|una|doi|două|trei|patru|cinci|șase|șapte|opt|nouă|zece|unsprezece|doisprezece|"
                r"douăzeci|treizeci|patruzeci|cincizeci|sut\w+|mie|mii|prim\w+|al doilea|ultim\w+)\b",
    "English": r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|"
               r"forty|fifty|hundred|thousand|first|second|third|last)\b",
}

# Безликое обозначение человека (27.09.2026 tekst-huka, аудит 2, «Хук и первые 2–5 минут»):
# донор зовёт героиню «старшей сестрой», хук — «женщиной», и картинки отдают роль чужой
# героине. Слово сверяется с полом героев хука из скелета: «женщина» — замечание, только если
# героинь в хуке две и больше (какую рисовать — картинка угадывает). Правило одно на все языки:
# считаем существительные «женщина / мужчина / девушка / парень / девочка / мальчик / ребёнок /
# старуха / человек» и «женская / мужская»; возраст, который бывает и прилагательным при роли
# («tânărul prinț», «el anciano rey», «il giovane principe»), не считаем. f/m — пол слова, n — любой.
БЕЗЛИКИЕ = {
    "Russian": {"f": r"женщин\w*|девушк\w*|девочк\w*|старух\w*|старушк\w*|женск\w*",
                "m": r"мужчин\w*|парень|парн(я|ю|ем|е|и|ей|ям|ями|ях)|юнош\w*|мальчик\w*|мальчишк\w*|стари[кч]\w*|мужск\w*",
                "n": r"человек|человека|человеку|человеком|человеке|ребён\w*|ребен\w*|дитя"},
    "Polish": {"f": r"kobiet\w*|kobiec\w*|dziewczyn\w*|staruszk[aięą]|staruszce",
               "m": r"mężczyzn\w*|męsk\w*|chłopak\w*|chłopiec|chłopc\w*|młodzie(niec|ńca|ńcowi|ńcem|ńcze|ńcy|ńców)|facet\w*|staruszek|starzec|starca|starcowi|starcem",
               "n": r"człowiek|człowieka|człowiekowi|człowiekiem|człowieku|dzieck\w*"},
    "Czech": {"f": r"žena|ženy|ženě|ženu|ženou|žen|ženám|ženami|žensk\w*|dívk\w*|holk\w*|holčičk\w*|stařen\w*",
              "m": r"muž|muže|muži|mužem|mužů|mužům|mužsk\w*|chlap|chlapa|chlapovi|chlapem|chlapi|chlap(e)?c\w*|kluk\w*|mladík\w*|stařec|starce|starci|stařík\w*",
              "n": r"člověk|člověka|člověku|člověkem|dítě|dítěte|dítěti|dítětem"},
    "Portuguese": {"f": r"mulher|mulheres|moça|moças|garota|garotas|menina|meninas|velhinha|velhinhas|feminin\w*",
                   "m": r"homem|homens|rapaz|rapazes|garoto|garotos|menino|meninos|velhinho|velhinhos|masculin\w*",
                   "n": r"pessoa|criança"},
    "Spanish": {"f": r"mujer|mujeres|chica|chicas|muchacha|muchachas|niña|niñas|viejita|femenin\w*",
                "m": r"hombre|hombres|chico|chicos|muchacho|muchachos|niño|niños|viejito|masculin\w*",
                "n": r"persona"},
    "Italian": {"f": r"donna|donne|ragazza|ragazze|bambina|bambine|vecchietta|femminil\w*",
                "m": r"uomo|uomini|ragazzo|ragazzi|bambino|bambini|vecchietto|maschil\w*",
                "n": r"persona"},
    "Romanian": {"f": r"femeie|femeia|femeii|femei|femeile|femeilor|fată|fata|fetei|fete|fetele|fetiț\w*|feminin\w*",
                 "m": r"bărbat|bărbatul|bărbatului|bărbați|bărbații|băiat|băiatul|băiatului|băieți|băieții|băiețel\w*|masculin\w*",
                 "n": r"om|omul|omului|copil|copilul|copilului"},
    "English": {"f": r"woman|women|girl|girls|female", "m": r"man|men|guy|guys|boy|boys|male",
                "n": r"person|child|kid"},
}
# Не безлико: притяжательное рядом — это родство («su mujer» — жена, «fata lui» — дочь,
# «со своей девушкой»); «единственный» / «ни один» до слова; сравнение в той же части фразы
# («словно на чужого человека»). Кортеж: притяжательное до слова, после слова, обобщение, сравнение.
НЕ_ЛИЦО = {
    "Russian": (r"его|её|ее|их|сво(й|я|ю|ё|е|ей|его|ему|им|их|ими)|мо(й|я|ю|ё|е|ей|его|ему|им|их)|"
                r"тво(й|я|ю|ё|е|ей|его|ему|им|их)|наш\w*|ваш\w*", "",
                r"единственн\w*|ни|никак\w*", r"словно|будто|подобно|как на|как к"),
    "Polish": (r"jego|jej|ich|swój|swoj\w+|swą|swe|swej|swym|swych|mój|moj\w+|mą|twój|twoj\w+|nasz\w*|wasz\w*", "",
               r"jedyn\w*|żad\w*", r"jakby|niczym|jak gdyby|jak na|jakoby"),
    "Czech": (r"jeho|její|jejich|svůj|svou|svoje|svá|svoji|své|svého|svému|svým|svých|můj|moje|mou|má|mého|mému|"
              r"tvůj|tvoje|tvá|tvou|náš|naš\w+|váš|vaš\w+", "",
              r"jedin\w*|žádn\w*", r"jakoby|jako by|jako kdyby|jako"),
    "Portuguese": (r"sua|suas|seu|seus|minha|meu|tua|teu|nossa|nosso", r"dele|dela|deles|delas",
                   r"únic[oa]s?|nenhum|nenhuma", r"como se|como a|como um|como uma|feito"),
    "Spanish": (r"su|sus|mi|mis|tu|tus|nuestr[oa]|vuestr[oa]", r"suy[oa]|mí[oa]",
                r"únic[oa]s?|ningún|ninguna|ninguno", r"como si|como quien|como a|como un|como una"),
    "Italian": (r"sua|suo|sue|suoi|mia|mio|tua|tuo|nostr[oa]|vostr[oa]|loro", "",
                r"unic[oaie]|nessun[oa]?", r"come se|come un|come una|come a|quasi"),
    "Romanian": ("", r"lui|ei|lor|sa|său|mea|meu|ta|tău|noastră|nostru|voastră|vostru",
                 r"singur\w*|niciun\w*|nicio", r"ca și cum|parcă|ca pe|ca un|ca o"),
    "English": (r"his|her|their|my|your|our", "", r"only|no|none", r"as if|as though|like"),
}

# Метка героя (28.09.2026 geroi, проба 9f8b8a20): «Герой уже однажды лежал бездыханным», «толкнула
# героя прочь» — безликая метка, как MC/GF на обложках. Считается при любом составе хука; только
# единственное число (множественное — не метка одного лица). Притяжательное метку не снимает:
# «наш герой», «Eroul nostru» — та же метка. «Протагонист» и «главный персонаж» — на всех языках.
ГЕРОЙ = {
    "Russian": r"геро(?:й|я|ю|ем|е)|героин(?:я|и|е|ю|ей|ею)",
    "Polish": r"bohater|bohatera|bohaterowi|bohaterem|bohaterze|bohaterk(?:a|i|ę|ą|o)|bohaterce",
    "Czech": r"hrdin(?:a|y|ovi|u|o|ou)|hrdink(?:a|y|u|o|ou)|hrdince",
    "Portuguese": r"herói|heroína", "Spanish": r"héroe|heroína", "Italian": r"eroe|eroina",
    "Romanian": r"erou|eroul|eroului|eroule|eroin(?:ă|a|ei|o)",
    "English": r"hero|heroine",
}
ГЛАВНЫЙ = {
    "Russian": r"главн\w*\s+персонаж\w*", "Polish": r"główn\w*\s+posta(?:ć|ci|cią)",
    "Czech": r"hlavn\w*\s+postav(?:a|y|ě|u|ou)", "Spanish": r"personaje\s+principal",
    "Italian": r"personaggio\s+principale", "Portuguese": r"personagem\s+principal",
    "Romanian": r"personaj(?:ul|ului)?\s+principal(?:ul|ului)?", "English": r"main\s+character",
}
ПРОТАГОНИСТ = r"протагонист\w*|protagonist\w*"
# Не метка, а роль другого лица: слово героя при книге, игре, войне, державе — «героиня романа»,
# «la heroína de la novela», «герой войны», «Герой Империи» (связка — как у владельца-лица, ЧЕЙ);
# в кавычках — речь людей истории или название («Героиня в изгнании»); творительный без предлога —
# сказуемое («умер героем», «считали героем»). «Герой нашей истории» — метка рассказчика: «история» не тут.
МИР = {
    "Russian": r"роман(?:а|ов)|игр(?:ы)?|книг(?:и)?|сказ(?:ки|ок|ания|аний)|легенд(?:ы)?|новелл(?:ы)?|"
               r"манхв(?:ы)?|манг(?:и)?|фильма|сериала|войн(?:ы)?|битв(?:ы)?|сражени(?:я|й)|импери(?:и|й)|"
               r"королевства|царства|страны|народа|государства|мира",
    "Polish": r"powieści|gry|gier|książ(?:ki|ek)|bajki|baśni|legend(?:y)?|mangi|manhwy|filmu|serialu|"
              r"wojn(?:y)|wojen|bitwy|imperium|cesarstwa|królestwa|kraju|narodu|państwa|świata",
    "Czech": r"román(?:u|ů)|hry|her|knih(?:y)?|pohádky|pověsti|legend(?:y)?|mangy|manhwy|filmu|seriálu|"
             r"války|válek|bitvy|říše|impéria|císařství|království|země|národa|státu|světa",
    "Portuguese": r"romances?|jogos?|livros?|conto|lenda|guerra|batalha|filme|série|novela|manhwa|mangá|"
                  r"manga|império|reino|país|povo|nação|mundo",
    "Spanish": r"novelas?|juegos?|libros?|cuento|leyenda|guerra|batalla|película|serie|manhwa|manga|"
               r"imperio|reino|país|pueblo|nación|mundo",
    "Italian": r"romanz[oi]|gioc(?:o|hi)|libr[oi]|fiaba|leggenda|guerra|battaglia|film|serie|manhwa|manga|"
               r"impero|regno|paese|popolo|nazione|mondo",
    "Romanian": r"romanului|jocului|căr[țţ]ii|pove[șş]tii|legendei|războiului|bătăliei|filmului|serialului|"
                r"imperiului|regatului|[țţ]ării|poporului|na[țţ]iunii|lumii",
    "English": r"novel|game|book|tale|legend|war|battle|film|movie|series|manhwa|manga|empire|kingdom|world",
}
ТВОР = {"Russian": (r"(?:ем|ей|ею)", r"с|со|за|перед|передо|над|надо|под|подо|между|рядом"),
        "Polish": (r"(?:em|ą)", r"z|ze|przed|za|nad|pod|między"),
        "Czech": (r"(?:ou)", r"s|se|před|za|nad|pod|mezi")}
ЦИТАТА = r'«[^»]*(?:»|$)|„[^“”]*(?:[“”]|$)|“[^”]*(?:”|$)|"[^"]*(?:"|$)|^[^«»]*»'
# «Стал другим человеком», «стал героем» — сказуемое, не метка (у славянских — творительный, между
# глаголом и словом — только определения: «стал перед женщиной» — замечание). Во фразе-вопросе
# («Che cosa diventerà un bambino…?») слово после глагола — подлежащее: не прощаем.
СТАЛ = {
    "Russian": r"\b(?:ст(?:ал|ала|ало|али|ать|анет|анешь|ану|анут|ановится|ановятся|ановился|ановилась|"
               r"ановились)|сделал(?:ся|ась|ись)|оказал(?:ся|ась|ись))"
               r"(?:\s+(?:[^\W\d_]*(?:ым|им|ой|ей|ою|ею|ом|ем)|его|её|ее|их)){0,2}\s+[^\W\d_]*(?:ом|ем|ой|ей|ою|ею)$",
    "Polish": r"\b(?:sta(?:ł|ła|ło|li|ły|je|ją|nie|ną)\s+się|się\s+sta(?:ł|ła|ło|li|ły|je|ją|nie|ną)|"
              r"zosta(?:ł|ła|ło|li|ły|nie|ną|ć))(?:\s+(?:[^\W\d_]*(?:ym|im|ą)|jego|jej|ich)){0,2}\s+[^\W\d_]*(?:em|ą)$",
    "Czech": r"\b(?:sta(?:l|la|lo|li|ly|ne|nou)\s+se|se\s+sta(?:l|la|lo|li|ly|ne|nou))"
             r"(?:\s+(?:[^\W\d_]*(?:ým|ím|ou)|jeho|její|jejich)){0,2}\s+[^\W\d_]*(?:em|ou|ím)$",
    "Portuguese": r"\b(?:torn\w*[\s-]+se|se\s+torn\w*)(?:\s+[^\W\d_]+){1,3}$",
    "Spanish": r"\b(?:convirti\w*|conviert\w*|convert\w*)\s+en(?:\s+[^\W\d_]+){1,3}$",
    "Italian": r"\b(?:divent\w*|divien\w*|divenn\w*)(?:[\s'’]+[^\W\d_]+){1,3}$",
    "Romanian": r"\b(?:deven\w*|devin\w*)(?:\s+[^\W\d_]+){1,3}$",
    "English": r"\b(?:became|become|becomes|becoming)(?:\s+[^\W\d_]+){1,3}$",
}
# «Двадцать человек погибли»: «человек» после числа — родительный множественного, это толпа, а не
# лицо; у польского и испанского там другое слово («ludzi», «personas»), и оно не считается.
СЧЁТ = {
    "Russian": r"дв(?:а|е|ух|ум|умя)|тр(?:и|ёх|ех|ём|ем|емя)|четыр(?:е|ёх|ех|ём|ем|ьмя)|пят(?:ь|и|ью)|"
               r"шест(?:ь|и|ью)|сем(?:ь|и)|восем(?:ь|ью)|восьми|девят(?:ь|и|ью)|десят(?:ь|и|ью)|\w+надцат(?:ь|и|ью)|"
               r"(?:два|три)дцат(?:ь|и|ью)|сорок|сорока|\w+десят(?:и|ью)?|девяност[оа]|сто|ста|сотн\w*|"
               r"двест[иа]|\w+сот|триста|четыреста|тысяч\w*|миллион\w*|двое|трое|четверо|пятеро|двоих|троих|"
               r"нескольк(?:о|их|им|ими)|много|многих|немало|сколько|столько|десятк\w*|дюжин\w*",
}
# Владелец-лицо (28.09.2026 geroi): «La mujer del duque», «A mulher do duque», «Žena vévody»,
# «Femeia ducelui», «La donna del duca» — отношение, как «жена герцога»; было замечание только
# не у русского. Владелец — роль (знать, власть, родня, начальство) или имя с большой буквы после
# связки: предлог с артиклем и притяжательным (de/del/do/da/della/di, of), у языков с родительным
# падежом — ничего либо определения в родительном («его старшего брата»). Имя без связки —
# приложение («Женщина Ли Мэй»), не владелец. У румынского роль — только в родительном («ducelui»):
# иначе «Femeia sorbea», «contempla» принимались за владельца.
# «La mujer del vestido rojo», «Мужчина средних лет» остаются замечанием: владелец не лицо.
ЧЕЙ = {
    "Russian": (r"(?:(?:[^\W\d_]*(?:ого|его|ой|ей|ых|их)|её|ее)\s+){0,2}",
                r"герцог\w*|княз\w*|княгин\w*|княжн\w*|корол\w*|цар[ьяюеи]\w*|императ\w*|граф\w*|барон\w*|"
                r"маркиз\w*|лорд\w*|господ\w*|госпож\w*|хозя\w*|босс\w*|начальни\w*|директор\w*|президент\w*|"
                r"генерал\w*|капитан\w*|командир\w*|вожд\w*|муж(?:а|у|ем|ья|ей|ьями?)|жен(?:ы|е|у|ой)|отц\w*|"
                r"отец|матер\w*|мам\w*|пап\w*|брат\w*|сестр\w*|сын\w*|дочер\w*|дочк\w*|дяд\w*|тёт\w*|тет[иеюй]\w*|"
                r"деда|дедушк\w*|бабушк\w*|внук\w*|внучк\w*|жених\w*|невест\w*|семь[иеёю]\w*|семейств\w*|клан\w*|"
                r"рода|наследни\w*|друг[аеу]|другом|друзей|подруг\w*|сосед\w*|учител\w*|наставни\w*|"
                r"патриарх\w*|глав[ыеу]"),
    "Polish": (r"(?:[^\W\d_]*(?:ego|ej|ych|ich)\s+){0,2}",
               r"książ\w*|księci\w*|księżn\w*|król\w*|cesarz\w*|hrabi\w*|baron\w*|markiz\w*|lord\w*|pan(?:a|u|em)?|"
               r"pani|władc\w*|szef\w*|prezes\w*|dyrektor\w*|prezydent\w*|generał\w*|kapitan\w*|dowódc\w*|wodz\w*|"
               r"mę(?:ża|żowi|żem)|żon(?:y|ie|ę|ą)|ojc\w*|matk\w*|mam\w*|tat\w*|brat\w*|braci\w*|siostr\w*|syn\w*|"
               r"córk\w*|wuj\w*|stryj\w*|ciotk\w*|cioci\w*|dziad\w*|babci\w*|babk\w*|wnu\w*|narzeczon\w*|rodzin\w*|"
               r"klan\w*|rodu|dziedzic\w*|przyjaci\w*|sąsiad\w*|nauczyciel\w*|mistrz\w*|patriarch\w*|głow(?:y|ie)"),
    "Czech": (r"(?:(?:[^\W\d_]*(?:ého|ho|é|ích|ých)|jeho|její|jejich)\s+){0,2}",
              r"vévod\w*|kníž\w*|princ\w*|král\w*|císař\w*|hrab\w*|baron\w*|markýz\w*|lord\w*|pán(?:a|ovi|em)?|"
              r"paní|vládc\w*|šéf\w*|ředitel\w*|prezident\w*|generál\w*|kapitán\w*|velitel\w*|vůdc\w*|manžel\w*|"
              r"otc\w*|otec|matk\w*|mámy|maminky|tát\w*|tatínk\w*|bratr\w*|sestr\w*|syn\w*|dcer\w*|strýc\w*|"
              r"tet\w*|děd\w*|babičk\w*|vnuk\w*|vnučk\w*|snouben\w*|rodin\w*|klan\w*|rodu|dědic\w*|přítel\w*|"
              r"kamarád\w*|soused\w*|učitel\w*|mistr\w*|patriarch\w*|hlav[yě]"),
    "Portuguese": (r"(?:do|da|dos|das|de)\s+(?:(?:seu|sua|seus|suas|meu|minha|teu|tua|nosso|nossa)\s+)?",
                   r"duque\w*|duquesa\w*|rei|reis|rainha\w*|príncipe\w*|princesa\w*|imperador\w*|imperatriz|"
                   r"conde\w*|barão|baronesa|marqu[eê]s\w*|lorde\w*|senhor\w*|amo|patrão|patroa|soberan\w*|"
                   r"chefe\w*|presidente\w*|diretor\w*|general\w*|capitão|comandante\w*|líder\w*|marido\w*|"
                   r"espos[oa]s?|pai|pais|mãe|mães|papai|mamãe|irmão\w*|irmã\w*|filh[oa]s?|ti[oa]s?|av[ôó]s?|"
                   r"net[oa]s?|noiv[oa]s?|namorad[oa]s?|família\w*|clã|casa|herdeir[oa]s?|amig[oa]s?|"
                   r"vizinh[oa]s?|mestre\w*|patriarca\w*"),
    "Spanish": (r"(?:del|de(?:\s+(?:la|las|los|el))?)\s+(?:(?:su|sus|mi|mis|tu|tus|nuestr[oa]s?)\s+)?",
                r"duque\w*|duquesa\w*|rey|reyes|reina\w*|príncipe\w*|princesa\w*|emperador\w*|emperatriz|"
                r"conde\w*|barón|baron\w*|marqu[eé]s\w*|lord\w*|señor\w*|amo|ama|amos|soberan\w*|jef[ea]\w*|"
                r"presidente\w*|director\w*|general\w*|capitán\w*|comandante\w*|líder\w*|espos[oa]s?|marido\w*|"
                r"padre\w*|madre\w*|papá|mamá|herman[oa]s?|hij[oa]s?|tí[oa]s?|abuel[oa]s?|niet[oa]s?|"
                r"prometid[oa]s?|novi[oa]s?|familia\w*|clan\w*|casa|hereder[oa]s?|amig[oa]s?|vecin[oa]s?|"
                r"maestr[oa]s?|patriarca\w*"),
    "Italian": (r"(?:del|dello|della|dei|degli|delle|di)\s+(?:(?:suo|sua|suoi|sue|mio|mia|tuo|tua|nostr[oa])\s+)?"
                r"|dell['’]|d['’]",
                r"duca|duchi|duchessa\w*|re|regina\w*|principe\w*|principessa\w*|imperator\w*|imperatric\w*|"
                r"conte|conti|contessa\w*|baron\w*|marches[ea]\w*|lord|signor\w*|padron\w*|sovran\w*|capo|capi|"
                r"boss|president\w*|direttor\w*|general\w*|capitan\w*|comandant\w*|marit[oi]|spos[oa]|moglie|"
                r"padre|padri|madre|madri|papà|mamma|fratell\w*|sorell\w*|figli\w*|zi[oaie]|nonn[oaie]|nipot\w*|"
                r"fidanzat\w*|famiglia\w*|clan|casat[oa]|ered[ei]|amic\w*|vicin\w*|maestr\w*|patriarca\w*"),
    "Romanian": (r"(?:(?:a|al|ale|ai)\s+)?",
                 r"(?:duce|rege|regin|prin[țţ]|prin[țţ]es|împărat|împărătes|conte|contes|baron|marchiz|lord|domn|"
                 r"doamn|stăpân|suveran|[șş]ef|pre[șş]edinte|director|general|căpitan|comandant|lider|so[țţ]|"
                 r"tat[ăa]|mam|frate|surori|fi|fiic|unchi|mătu[șş]|bunic|nepo[tț]|nepoat|logodnic|famili|clan|"
                 r"mo[șş]tenitor|prieten|vecin|profesor|maestr|patriarh)(?:lui|ului|ei|iei|ii|ilor|elor|i)"),
    "English": (r"of\s+(?:(?:the|his|her|their|my|your|our)\s+)?",
                r"(?:duke|duchess|king|queen|prince|princess|emperor|empress|count|countess|earl|baron|marquis|"
                r"lord|lady|master|mistress|boss|chief|president|director|general|captain|commander|husband|wife|"
                r"father|mother|dad|mom|brother|sister|son|daughter|uncle|aunt|grandfather|grandmother|fianc[ée]e?|"
                r"family|clan|house|heir|friend|neighbou?r|teacher)s?"),
}
ПРОПИСНЫЕ = "A-ZÀ-ÖØ-ÞĂĄĆČĎĘĚŁŃŇŘŚŞŠŢŤŮŹŻŽȘȚА-ЯЁ"


def чей(хвост: str, lang: str, ещё: str = "") -> bool:
    """За словом — владелец-лицо: роль («del duque», «vévody») или имя после связки («de Carlos»).
    ещё — что ещё считается владельцем (у метки героя — книга, игра, война, держава)."""
    связка, роль = ЧЕЙ.get(lang, ЧЕЙ["English"])
    m = re.match(r"\s*(%s)(?:(?:%s)\b|(?-i:([%s])))" % (связка, роль + ("|" + ещё if ещё else ""), ПРОПИСНЫЕ),
                 хвост, re.IGNORECASE)
    return bool(m) and not (m.group(2) and not m.group(1).strip())


def безликие_в(фразы, lang: str, полы, герой_в_мире: bool = False) -> list:
    """Безликие обозначения людей: ["5 «Женщина»", …]. полы — пол каждого героя хука.
    герой_в_мире — у источника «the hero» — роль другого лица (герой игры): метку героя не считаем."""
    слова_, (до, после, общее, как) = БЕЗЛИКИЕ.get(lang, БЕЗЛИКИЕ["English"]), НЕ_ЛИЦО.get(lang, НЕ_ЛИЦО["English"])
    слова_ = dict(слова_, p=ПРОТАГОНИСТ + "|" + ГЛАВНЫЙ.get(lang, ГЛАВНЫЙ["English"]),
                  **({} if герой_в_мире else {"h": ГЕРОЙ.get(lang, ГЕРОЙ["English"])}))
    стал, счёт = СТАЛ.get(lang, СТАЛ["English"]), СЧЁТ.get(lang, r"(?!)")
    мир, (твор, предлоги) = МИР.get(lang, МИР["English"]), ТВОР.get(lang, (r"(?!)", ""))
    найдено = []
    for n, p in enumerate(фразы, 1):
        for g, рег in слова_.items():
            if g in "fmn" and (полы.count(g) if g != "n" else max(полы.count("f"), полы.count("m"))) < 2:
                continue
            for m in re.finditer(r"\b(?:%s)\b" % рег, p, re.IGNORECASE):
                слева = re.findall(r"[^\W\d_]+", p[:m.start()])
                справа = re.findall(r"[^\W\d_]+", p[m.end():])[:1]
                часть = re.split(r"[,;:—–(]", p[:m.start()])[-1]
                метка = g in "hp"      # метка героя: притяжательное не снимает («наш герой»)
                if ((not метка and до and слева and re.fullmatch(до, слева[-1], re.IGNORECASE))
                        or (not метка and после and справа and re.fullmatch(после, справа[0], re.IGNORECASE))
                        or (метка and (чей(p[m.end():], lang, мир)
                                       or any(q.start() <= m.start() < q.end() for q in re.finditer(ЦИТАТА, p))
                                       or (re.search(твор + "$", m.group(0), re.IGNORECASE) and not
                                           (слева and re.fullmatch(предлоги, слева[-1], re.IGNORECASE)))))
                        or any(re.fullmatch(общее, w, re.IGNORECASE) for w in слева[-3:])
                        or re.search(r"\b(?:%s)\b" % как, часть, re.IGNORECASE)
                        or ("?" not in p and re.search(стал, re.split(r"[,;:—–(]", p[:m.end()])[-1], re.IGNORECASE))
                        or (g == "n" and re.search(r"(?:\d|\b(?:%s))\s*$" % счёт, p[:m.start()], re.IGNORECASE))
                        or чей(p[m.end():], lang)):
                    continue
                найдено.append("%d «%s»" % (n, m.group(0)))
    return найдено


ЦЕЛИ = {
    "картинок в первые 30 с": 9,
    "картинок в первые 90 с": 24,
}
# Длина фраз хука (hookv2, 28.09.2026). Записка v2 просит длину разную, как в речи: большинство фраз
# в коридоре, несколько ударов по 2–3 слова, 4–8 длинных по 15–20 слов из двух половин. Прежняя
# проверка «в коридоре 22 фразы из 30» требовала ровную дробь, и слепые судьи узнавали по ней шаблон
# (у конкурентов в первые 90 с медиана 8–16 слов, разброс больше 8 слов у 21 из 23). Теперь мягко:
# медиана первых 30 фраз в коридоре и разброс длины больше РАЗБРОС слов. «Число или срок» и «прямая
# речь» в первых 10 фразах — сведения: v2 берёт их, только если они есть у источника. «Суть истории
# к 12-й фразе» без модели не проверить — не проверяем.
РАЗБРОС = 8


def фразы(текст: str, сколько: int = 40):
    куски = re.split(r"(?<=[.!?…»])\s+", текст.strip())
    return [к.strip() for к in куски if к.strip()][:сколько]


# Разрез для длины фраз хука (hookv2). фразы() режет и на закрывающей кавычке внутри фразы: «Игра
# «Эдиус» началась» давала куски «Игра «Эдиус»» и «началась» — ложные фразы из 1–2 слов, и разброс
# длины выходил ложным. Здесь режем только после . ! ? … (с закрывающей кавычкой или без), если
# дальше заглавная буква или цифра: «„Стой!“ — крикнул он» — одна фраза. Прочие проверки считают по
# фразы(), как калиброваны.
_КОНЕЦ_ФРАЗЫ = re.compile(r"(?:(?<=[.!?…])|(?<=[.!?…][»”\"’]))\s+(?=(?:[—–-]\s*)?[«„“\"(¡¿]*[%s\d])"
                          % ПРОПИСНЫЕ)


def фразы_хука(текст: str, сколько: int = 30):
    куски = _КОНЕЦ_ФРАЗЫ.split(текст.strip())
    return [к.strip() for к in куски if к.strip()][:сколько]


def слов(фраза: str) -> int:
    return len(re.findall(r"[^\W\d_]+", фраза, re.UNICODE))


def картинок_за(frames, секунд: float) -> int:
    t, n = 0.0, 0
    for f in frames:
        if t >= секунд:
            break
        n += 1
        t += float(f.get("seconds") or 0)
    return n


# Повтор хука (povtor-huka 27–28.09.2026): замер effort_ab — хук показал событие целиком, дальше
# в сцене оно рассказано заново по порядку, а приёмка это пропустила. Двойник фразы хука 11–30 —
# фраза дальше, с которой общие ≥2 основы и не меньше половины её основ (основа — первые 4 буквы
# слова от 4 букв; имена не в счёт; служебное слово — то, чьи первые 5 букв есть в >6% фраз ролика,
# но не меньше чем в 10 фразах, — тоже). Служебность — по 5 буквам самого слова: у 4 букв «стол»
# сливалось со «столицей» и выпадало, и пересказ 460f2b0b проходил мимо (verify3_1). Считаем цепочку
# пар по порядку хука, где пересказ идёт в темпе пересказа: от пары к паре слов дальше не меньше,
# чем в хуке (пересказ разворачивает короткие фразы хука), и не больше чем в 5 раз + 20 слов.
# Продолжение сцены лишь ссылается на хук и пробегает его быстрее; случайные совпадения слов
# разбросаны. Окно — max(первая сцена по раскадровке, хук + 2 минуты голоса), без раскадровки —
# хук + 4 минуты. Фразы 1–10 — витрина: число и обещание записка велит вернуть.
# Калибровка v4 (28.09) на 95 роликах студии и 10 живых прогонах записки: у всех ≤2, замечаний 0;
# пересказы medium, es_new, ru_probe_v2_rep, ru_julian_v3 — 4 и больше. Порог 3 — посередине: одна
# пара запаса с обеих сторон (при пороге 4 пересказы стояли ровно на пороге). Пересказ другими
# словами (синонимами) так не ловится — его закрывает записка первой сцены.
ПОВТОР = 3


def повтор_хука(текст: str, wpm=None, слов_сцены=None):
    """([(n, m, фраза хука, двойник)] — цепочка по порядку, сколько фраз 11–30 проверено).
    слов_сцены — длина первой сцены по раскадровке; окно — max(она, хук + 2 минуты голоса),
    не известна — хук + 4 минуты. Без вызова модели."""
    from collections import Counter
    все = фразы(текст, 10 ** 9)
    сл = [re.findall(r"[^\W\d_]+", p) for p in все]
    строчные = {w[:4] for ws in сл for w in ws if w[:1].islower()}
    имена = {w.lower()[:4] for ws in сл for w in ws[1:] if w[:1].isupper()} - строчные
    слова = [{w.lower() for w in ws if len(w) >= 4 and w.lower()[:4] not in имена} for ws in сл]
    осн = [{w[:4] for w in s} for s in слова]
    df5 = Counter(н for ws in слова for н in {w[:5] for w in ws})   # в скольких фразах начало слова
    try:
        темп = float(wpm or 0) or 135.0
    except (TypeError, ValueError):
        темп = 135.0
    хук = sum(map(len, сл[:30]))
    предел = max(слов_сцены, хук + темп * 2.0) if слов_сцены else хук + темп * 4.0
    конец, n = 0, 0
    while конец < len(все) and n < предел:
        n += len(сл[конец])
        конец += 1
    служебные = max(10, 0.06 * len(все))
    пары = []
    for i in range(10, min(30, конец)):
        h = {w[:4] for w in слова[i] if df5[w[:5]] <= служебные}
        for j in range(30, конец):
            общих = len(h & осн[j])
            if общих >= 2 and 2 * общих >= len(h):
                пары.append((i, j))
    до = [0]   # до[k] — слов в фразах до k-й
    for ws in сл:
        до.append(до[-1] + len(ws))

    def темп_пересказа(a, b):
        в_хуке, дальше = до[b[0]] - до[a[0]], до[b[1]] - до[a[1]]
        return a[0] < b[0] and a[1] < b[1] and в_хуке <= дальше <= 5 * в_хуке + 20
    цепи = []   # самая длинная цепочка: номера растут и в хуке, и дальше, в темпе пересказа
    for t, (i, j) in enumerate(пары):
        цепи.append(max((цепи[u] for u in range(t) if темп_пересказа(пары[u], (i, j))),
                        key=len, default=[]) + [(i + 1, j + 1, все[i], все[j])])
    return max(цепи, key=len, default=[]), max(0, min(30, len(все)) - 10)


def run(проект: str) -> dict:
    """Отчёт приёмки хука по папке проекта."""
    lang = "Russian"
    pj = os.path.join(проект, "project.json")
    if os.path.exists(pj):
        lang = (json.load(open(pj, encoding="utf-8")).get("language") or "Russian").strip()
    текст = open(os.path.join(проект, "text", "final.txt"), encoding="utf-8").read()
    sb_path = os.path.join(проект, "text", "storyboard.json")
    sb = json.load(open(sb_path, encoding="utf-8")) if os.path.exists(sb_path) else {}
    frames = sb.get("frames") if isinstance(sb, dict) else (sb or [])
    frames = frames or []

    # Коридор длины фраз — тот же, что заказан модели: от скорости голоса ролика.
    wpm = None
    pace = os.path.join(проект, "text", "pace.json")
    if os.path.exists(pace):
        try:
            wpm = json.load(open(pace, encoding="utf-8")).get("wpm")
        except (OSError, ValueError):
            wpm = None
    try:
        import rewrite as _rw
        ло, хи = _rw.hook_corridor(wpm)
    except Exception:   # noqa: BLE001 — приёмка не должна падать из-за импорта
        ло, хи = 5, 9

    # Длина первой сцены — по кадрам раскадровки; у перевода кадры эталона, поэтому берём долю.
    с1 = sum(слов(f.get("text") or "") for f in frames if isinstance(f, dict) and f.get("scene_num") == 1)
    всех = sum(слов(f.get("text") or "") for f in frames if isinstance(f, dict))
    повтор, тело = повтор_хука(текст, wpm, с1 * слов(текст) / всех if с1 and всех else None)
    ф = фразы(текст)
    первая = ф[0] if ф else ""
    первое_слово = (re.findall(r"[^\W\d_]+", первая, re.UNICODE) or [""])[0].lower()
    приставки = МЕСТО.get(lang, МЕСТО["English"])
    начало_с_места = первое_слово in приставки

    первые30 = фразы_хука(текст, 30)
    длины = sorted(слов(p) for p in первые30)
    в_коридоре = sum(1 for n in длины if ло <= n <= хи)
    медиана = (длины[(len(длины) - 1) // 2] + длины[len(длины) // 2]) / 2 if длины else 0
    разброс = длины[-1] - длины[0] if длины else 0
    длинных = sum(1 for n in длины if n >= 15)

    первые10 = " ".join(ф[:10])
    есть_число = bool(re.search(r"\d", первые10)
                      or re.search(ЧИСЛА.get(lang, ЧИСЛА["English"]), первые10, re.IGNORECASE))
    есть_речь = bool(re.search(r"[«»\"„“”]", первые10) or re.search(r"(^|\s)[—–-]\s+\w", первые10))

    # Имена собственные: слово с большой буквы НЕ в начале фразы.
    имена = 0
    for p in ф[:10]:
        слова = re.findall(r"[^\W\d_]+", p, re.UNICODE)
        имена += sum(1 for w in слова[1:] if w[:1].isupper())

    # Люди в хуке — ролью (tekst-huka, 27.09.2026). Кто в хуке: первая сцена скелета и
    # первые 14 кадров; пол — из глоссария. Скелет — ответ модели как есть, поэтому его порча
    # не должна ронять остальные проверки приёмки.
    пол, состав, без_скелета = {}, set(), ""
    try:
        скелет = json.load(open(os.path.join(проект, "text", "skeleton.json"), encoding="utf-8"))
        пол = {str(c.get("id")): c.get("gender") for c in скелет.get("characters") or []}
        сцены = sorted(скелет.get("scenes") or [], key=lambda s: s.get("num") or 0)
        состав.update(str(x) for x in ((сцены[0].get("who") or []) if сцены else []))
    except Exception as e:   # noqa: BLE001 — нет скелета или он испорчен: остальные проверки идут
        # 28.09.2026 geroi: был pass — без полов проверка молча писала «да». Теперь «нет данных».
        без_скелета = "скелет не прочитан (%s: %s) — «люди в хуке» без данных" % (type(e).__name__, str(e)[:80])
    for f in frames[:14]:
        состав.update(str(x) for x in (f.get("in_frame") or []))
    # «the hero» / «the female lead» у источника — роль другого лица (герой игры 88971b07, героиня
    # романа у злодейки): «героя» в хуке — роль, не метка. У перевода источник — у эталона
    # (text/source_project.json, как pipeline.source_project).
    try:
        этал = json.load(open(os.path.join(проект, "text", "source_project.json"), encoding="utf-8"))["project"]
    except (OSError, ValueError, KeyError, TypeError):   # не перевод
        этал = ""
    папки = [проект] + ([os.path.join(os.path.dirname(os.path.abspath(проект)), str(этал))] if этал else [])
    исх = "".join(open(os.path.join(п, "text", "source.txt"), encoding="utf-8", errors="replace").read()
                  for п in папки if os.path.isfile(os.path.join(п, "text", "source.txt")))
    # 28.09.2026: 'female/male lead' не в счёт — в жанре «попал в роман злодеем» он есть почти всегда, и
    # проверка метки героя выключалась на весь ролик (проба 9f8b8a20: «Герой уже однажды лежал…» — «да»)
    в_мире = len(re.findall(r"\bthe (?:game's |novel's |original )*(?:main )?(?:hero(?:ine)?)\b",
                            исх, re.I)) >= 3
    безликие = безликие_в(ф[:14], lang, [пол.get(x) for x in состав], в_мире)

    # 29.09.2026 meta: служебная строка модели в тексте или в кадрах («Good, 858 words … Here is the
    # final narration:», 54706a7d) — голос прочтёт её. Новые сцены чистит rewrite.strip_meta; здесь — старые
    # тексты и переводы с копией раскадровки эталона. Ничего не блокирует: громко в журнал до картинок.
    служебные, сбой = [], ""
    try:
        import rewrite as _rw
        служебные = ["в тексте: «%s»" % л.strip() for л in текст.split("\n") if _rw.meta_line(л, lang)] + [
            "в кадре %s: «%s»" % (f.get("idx"), л.strip()) for f in frames if isinstance(f, dict)
            for л in (f.get("text") or "").split("\n") if _rw.meta_line(л, lang)]
    except Exception as e:   # noqa: BLE001 — приёмка не должна падать из-за импорта
        сбой = "проверка служебных строк не отработала: %s: %s" % (type(e).__name__, str(e)[:80])
    к30, к90 = картинок_за(frames, 30), картинок_за(frames, 90)
    # пустая вставка — предмет или место без людей (яд у чашки, пустой лагерь) — законна: такие
    # кадры раскадровка опустошает сама (аудит V2-04, 27.09.2026). Считаем только прочие
    пустой_состав = sum(1 for f in frames if not (f.get("in_frame") or [])
                        and not ((f.get("panel") or "").lower() in ("detail", "establishing")
                                 and not f.get("face_visible", True)))
    говорящий_за_кадром = sum(
        1 for f in frames
        if (f.get("quote") or "").strip() and (f.get("quote_by") or "").strip()
        and (f.get("quote_by") or "").strip() not in [str(x) for x in (f.get("in_frame") or [])])
    реплик_всего = sum(1 for f in frames if (f.get("quote") or "").strip())
    реплика_без_автора = sum(1 for f in frames
                             if (f.get("quote") or "").strip() and not (f.get("quote_by") or "").strip())

    # Первый кадр ролика не должен быть установочным общим планом: проба 27.09 открылась
    # спокойной гостиной под фразу «Старую госпожу убьют ежедневными дозами яда».
    к1 = frames[0] if frames else {}
    первый_установочный = ((к1.get("panel") or "").lower() == "establishing"
                           or (к1.get("shot_size") or "").lower() == "wide")
    первый_спокоен = (к1.get("emotion") or "").lower() in ("calm", "blank")
    # Реакция общим планом: лица мелкие, эмоцию не прочесть (проба 27.09, кадр 11
    # «Гости замерли» — «потрясение» в раскадровке, спокойные лица на картинке).
    реакций_общим = [f.get("idx") for f in frames[:14]
                     if (f.get("panel") or "").lower() in ("reaction", "face")
                     and (f.get("shot_size") or "").lower() in ("wide", "full")]
    длит = [float(f.get("seconds") or 0) for f in frames[:20]]
    первый_кадр = round(длит[0], 1) if длит else 0.0

    проверки = [
        ("не начинаем с места", not начало_с_места, "первое слово: «%s»" % первое_слово),
        (f"медиана длины фраз хука {ло}–{хи} слов", ло <= медиана <= хи,
         "%g слов; в коридоре %d из %d фраз" % (медиана, в_коридоре, len(первые30))),
        (f"длина фраз хука разная (разброс больше {РАЗБРОС} слов)", разброс > РАЗБРОС,
         "от %d до %d слов, длинных (15+) %d" % (длины[0] if длины else 0, длины[-1] if длины else 0,
                                               длинных)),
        ("имён собственных в первых 10 фразах нет", имена == 0, "%d" % имена),
        ("люди в хуке названы ролью, не «женщина»", not безликие and not без_скелета,
         "; ".join(x for x in ("нет данных" if без_скелета else "",
                               ("%d: %s" % (len(безликие), ", ".join(безликие[:6]))) if безликие else "") if x)
         or "да"),
        ("картинок в первые 30 с", к30 >= ЦЕЛИ["картинок в первые 30 с"],
         "%d (норма ниши 9–14)" % к30),
        ("картинок в первые 90 с", к90 >= ЦЕЛИ["картинок в первые 90 с"], "%d" % к90),
        ("первый кадр не длиннее 3 с", первый_кадр <= 3.0, "%.1f с" % первый_кадр),
        ("первый кадр — не установочный план", not первый_установочный,
         "%s / %s" % (к1.get("shot_size") or "—", к1.get("panel") or "—")),
        ("первый кадр не спокойный", not первый_спокоен, к1.get("emotion") or "—"),
        ("реакций общим планом в хуке нет", not реакций_общим,
         ("кадры %s" % реакций_общим) if реакций_общим else "нет"),
        ("кадров без людей нет", пустой_состав == 0, "%d из %d" % (пустой_состав, len(frames))),
        # Говорящий за кадром — приём, а не брак: студия вставляет отдельный крупный план
        # говорящего. Но если таких реплик много, значит состав кадра угадан плохо.
        ("говорящих за кадром не больше четверти",
         говорящий_за_кадром <= max(1, 0.25 * max(1, реплик_всего)),
         "%d из %d реплик" % (говорящий_за_кадром, реплик_всего)),
        ("у реплики есть автор", реплика_без_автора == 0, "%d без автора" % реплика_без_автора),
    ]
    # Сведения — не проверки, в «прошло» не входят: записка v2 берёт число и реплику, только если
    # они есть у источника, и не на фиксированном месте.
    сведения = [
        ("число или срок в первых 10 фразах", "есть" if есть_число else "нет"),
        ("прямая речь в первых 10 фразах", "есть" if есть_речь else "нет"),
    ]
    прошло = sum(1 for _, ok, _ in проверки if ok)
    return {
        "язык": lang,
        "журнал": [x for x in (без_скелета, сбой) if x]
                 + ["служебная строка модели %s — убрать до озвучки" % x for x in служебные],
        "служебные_строки": служебные,
        "первая_фраза": первая[:160],
        "прошло": прошло,
        "всего": len(проверки),
        "проверки": [{"что": n, "ок": bool(ok), "значение": v} for n, ok, v in проверки],
        "сведения": [{"что": n, "значение": v} for n, v in сведения],
        "повтор_хука": {"ок": len(повтор) < ПОВТОР, "пар_по_порядку": len(повтор), "порог": ПОВТОР,
                        "фраз_хука": тело, "пары": [{"фраза": a, "двойник": b, "хук": x, "дальше": y}
                                                     for a, b, x, y in повтор]},
        "числа": {"картинок_30с": к30, "картинок_90с": к90, "первый_кадр_с": первый_кадр,
                  "фраз_в_коридоре": в_коридоре, "медиана_слов": медиана, "разброс_слов": разброс,
                  "длинных_15": длинных,
                  "кадров": len(frames), "пустой_состав": пустой_состав,
                  "говорящий_за_кадром": говорящий_за_кадром, "реплик": реплик_всего},
    }


def печать(rep, log=print):
    log("  приёмка хука [%s]: прошло %d из %d" % (rep["язык"], rep["прошло"], rep["всего"]))
    log("  первая фраза: %s" % rep["первая_фраза"])
    for x in rep.get("журнал") or []:
        log("  ! приёмка хука: %s" % x)
    for c in rep["проверки"]:
        log("   %s %-38s %s" % ("да " if c["ок"] else "НЕТ", c["что"], c["значение"]))
    for c in rep.get("сведения") or []:
        log("    ·  %-38s %s" % (c["что"], c["значение"]))
    п = rep.get("повтор_хука")
    if п:
        log("   %s %-38s %s" % ("да " if п["ок"] else "НЕТ", "хук не пересказан заново (отдельно)",
                              "%d пар по порядку, порог %d" % (п["пар_по_порядку"], п["порог"])))
        for x in ([] if п["ок"] else п["пары"][:4]):
            log("     %d «%s» ~ %d «%s»" % (x["фраза"], x["хук"][:70], x["двойник"], x["дальше"][:90]))


if __name__ == "__main__":
    for d in sys.argv[1:]:
        r = run(d)
        print("=" * 76)
        print(os.path.basename(d.rstrip("/"))[:8])
        печать(r)
