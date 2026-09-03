# Reference tests

Это выборка тестов, которая показывает важные текущие контракты: fairness, run tokens,
partial/resume, reference durability, publication state и upload receipts.

Она не является единым готовым suite: тесты взяты из разных working trees, используют разные
imports/fixtures и могут не совпадать с production snapshot. Их задача — помочь написать чистые
contract tests новой реализации. Запускать их следует только в изолированном environment после
явной сборки зависимостей; `tools/package_check.py` их не импортирует и не исполняет.
