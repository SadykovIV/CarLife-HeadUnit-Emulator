# CarLife по Wi-Fi

Проверено по публичным исходникам 9 ноября 2026 года. Эмулятор использует штатный шестиканальный сетевой формат CarLife. Телефон и компьютер должны иметь IP-связность, например через точку доступа телефона или общий Wi-Fi.

## Обзор реализаций

Поиск охватывал библиотеки HU/MD, настольные эмуляторы и телефонные мосты. Ниже — релевантные реализации; это не заявление о проверке каждого форка GitHub.

| Проект | Что проверено для этой задачи |
| --- | --- |
| [HeadSwap](https://github.com/YegorkaPomidorka/headswap) и [HeadSwap Desktop](https://github.com/SadykovIV/headswap_desk) | Фактический сервер телефона, готовность шести каналов, discovery и доступный пользователю запуск Wi-Fi. Закреплённые ссылки приведены ниже. |
| [FT](https://github.com/OneAboveAll1964/ft-app) | Различие IP-транспорта и Bluetooth/Wi-Fi Direct bootstrap, дополнительный UDP-ответчик. |
| [carlifeLIB](https://github.com/qiyunjzz/carlifeLIB) | Классическая C++ сторона головного устройства: порты и сетевое подключение. |
| [CarLife-MD-Lib](https://github.com/Doris-duo/CarLife-MD-Lib) | Библиотека противоположной, мобильной стороны; README указывает происхождение из Apollo CarLife-Vehicle-Lib. |
| [carlife_pc_tool](https://github.com/MeshHun/carlife_pc_tool#readme) | Независимое подтверждение LAN discovery через `CarlifeHost`/7999 и заголовков 8/12 байт. Этот проект также открывает DATA/9440 и описывает согласование v4; его набор каналов и handshake не подменяют профиль Honda v2 для HeadSwap. |
| [mcarplay](https://github.com/wangxso/mcarplay#readme) | Смежный мост Xiaomi CarWith → CarPlay. Его семь каналов и RSA/AES-согласование относятся к собственному источнику CarWith; они не являются требованиями сервера HeadSwap. |

Реализация транспорта эмулятора сохраняет собственный код сессии и декодирования. Совместимость с HeadSwap проверяется по его сетевому контракту, без зависимости от сторонних бинарных библиотек этих проектов.

## Обнаружение и подключение

Телефон слушает TCP-порты. Головное устройство открывает к одному IP все шесть соединений с одного своего адреса, затем начинает согласование CarLife.

| Канал | TCP-порт телефона |
| --- | --- |
| CMD | 7240 |
| VIDEO | 8240 |
| MEDIA | 9240 |
| TTS | 9241 |
| VR | 9242 |
| TOUCH | 9340 |

HeadSwap начинает сеанс после готовности 6/6. Ошибка одного канала завершает весь транспорт. Дополнительный DATA 9440 не требуется этому эмулятору и не входит в готовность 6/6. Эти порты согласуются с классической клиентской библиотекой ГУ и текущим сервером HeadSwap. [Библиотека ГУ](https://github.com/qiyunjzz/carlifeLIB/blob/fdf8310bf1b6f0c4c247d848793832c016607561/carlifLib/carlifelibsourcecode/trunkbin/include/CConnectManager.h#L25), [HeadSwap](https://github.com/YegorkaPomidorka/headswap/blob/75bbc18deff6754b9b707dd18a5e60bce0a107fe/android/app/src/main/java/com/yegorkapomidorka/headswap/link/MultiSocketTransport.kt#L25)

Для обнаружения телефон посылает ASCII-датаграмму `CarlifeHost` на UDP 7999. Эмулятор получает адрес телефона из адреса отправителя. HeadSwap рассылает маяк каждые две секунды и прекращает после подключения каналов. При недоступном broadcast можно указать IP телефона вручную.

HeadSwap и FT также отвечают на дополнительный запрос `{"carlifehost":"carlife"}` к UDP 8999 сообщением `{"carlifehost":"carlife","status":"ready"}`. Это расширение для обнаружения. Наличие такого ответчика у любого приложения Baidu CarLife не подтверждено. [Маяк HeadSwap](https://github.com/YegorkaPomidorka/headswap/blob/75bbc18deff6754b9b707dd18a5e60bce0a107fe/android/app/src/main/java/com/yegorkapomidorka/headswap/link/wifi/HotspotBeacon.kt#L76), [Ответчик FT](https://github.com/OneAboveAll1964/ft-app/blob/bea43a64fe914e592ffe7c3040fbe3054171e140/app/src/main/java/app/ft/carlife/CarDiscovery.kt#L41)

## Формат и согласование

Все целые числа заголовка передаются в big-endian.

| Канал | Внутренний заголовок |
| --- | --- |
| CMD, TOUCH | Длина тела u16, reserved u16, идентификатор сообщения u32 |
| VIDEO, MEDIA, TTS, VR | Длина тела u32, timestamp u32, идентификатор сообщения u32 |

Каждое TCP-соединение передаёт последовательность внутренних заголовков и тел своего канала. Один TCP read может содержать часть сообщения или несколько сообщений. Внешние восемь байт с номером канала и длиной, используемые USB AOA и отладочным одиночным TCP-потоком, на сетевые порты телефона не отправляются. Внутри эмулятора адаптер восстанавливает это обрамление для общей обработки видео, звука, ввода и приборки.

После открытия шести соединений эмулятор отправляет HU_PROTOCOL_VERSION и использует обычное согласование профиля CarLife v2 Honda. Ответы и потоки читаются независимо по каналам. Heartbeat ГУ отправляется на VIDEO как пустое сообщение 0x20002 с 12-байтным заголовком раз в секунду. HeadSwap также принимает особый CMD heartbeat некоторых ГУ VW. Это наблюдение относится к приёму телефоном сообщений от автомобиля. [Парсер HeadSwap](https://github.com/YegorkaPomidorka/headswap/blob/75bbc18deff6754b9b707dd18a5e60bce0a107fe/android/app/src/main/java/com/yegorkapomidorka/headswap/link/MultiSocketTransport.kt#L37), [Независимый пример ГУ с TCP6 и discovery](https://github.com/YegorkaPomidorka/headswap/blob/75bbc18deff6754b9b707dd18a5e60bce0a107fe/pc-headunit/carlife_hu.py)

Bluetooth используется оригинальными беспроводными ГУ для начального обмена параметрами Wi-Fi Direct и IP телефона. При заранее установленной общей Wi-Fi-связи эмулятор может подключиться по IP или маяку. Его сетевой режим не создаёт группу Wi-Fi Direct и не реализует Bluetooth-согласование ГУ. [Последовательность FT](https://github.com/OneAboveAll1964/ft-app/blob/bea43a64fe914e592ffe7c3040fbe3054171e140/docs/wireless-connection.md)

## Подключение HeadSwap Desktop

Штатный пользовательский путь проверен по исходникам выпущенного HeadSwap Desktop 0.6.0.

1. Подготовьте доступ к рабочему столу на главном экране HeadSwap.
2. Подключите компьютер и телефон к общей сети либо компьютер к точке доступа телефона.
3. На главном экране выберите «Тип подключения» → «Wi-Fi», затем нажмите «Подключить».
4. При запросе откройте настройку беспроводного подключения, нажмите «Разрешить подключение», затем «Подключить по Wi-Fi».
5. В эмуляторе выберите Wi-Fi, найдите телефон или укажите его IP и подключитесь.

Для ручного режима Wi-Fi сохранённая Bluetooth-магнитола не требуется. Приложение запрашивает разрешения устройств поблизости и, на Android 17, локальной сети. Выбор «Автоматически» без сохранённой беспроводной магнитолы приводит к ожиданию USB, поэтому для эмулятора используйте явный Wi-Fi. В старом Desktop 0.5.1 беспроводной запуск был отключён параметром сборки. [Селектор 0.6.0](https://github.com/SadykovIV/headswap_desk/blob/145f4d5c76770f904783e100a2017ba7b4c9a52b/android/app/src/main/java/com/headswap/desk/dex/DexSetupActivity.kt#L541), [Условия запуска](https://github.com/SadykovIV/headswap_desk/blob/145f4d5c76770f904783e100a2017ba7b4c9a52b/android/app/src/main/java/com/headswap/desk/link/car/WirelessCarLife.kt#L61)

В оригинальном HeadSwap нужна сборка с беспроводным режимом и включённое ручное ожидание по Wi-Fi в настройках. Подготовка Android Auto выполняется в самом HeadSwap. После запуска ожидания используются те же шесть портов и маяк. [Исходники настроек HeadSwap](https://github.com/YegorkaPomidorka/headswap/blob/75bbc18deff6754b9b707dd18a5e60bce0a107fe/android/app/src/main/java/com/yegorkapomidorka/headswap/ui/SettingsScreen.kt)

## Подсказки и обложка

Музыкальные данные и обложка поступают с телефона в CarLife MEDIA_INFO по CMD. Показ обложки зависит от отправителя и текущего плеера. Desktop 0.6.0 извлекает доступный bitmap из MediaSession и передаёт его в CarLife. Для этого нужен доступ к медиа-сеансам через разрешённые уведомления или соответствующие системные права. Если плеер предоставляет только URL картинки или не предоставляет изображение, обложка может отсутствовать. Обработчик Desktop не загружает удалённые URL. [Источник метаданных Desktop](https://github.com/SadykovIV/headswap_desk/blob/145f4d5c76770f904783e100a2017ba7b4c9a52b/android/app/src/main/java/com/headswap/desk/cluster/ClusterMediaSource.kt#L58), [Передача в CarLife](https://github.com/SadykovIV/headswap_desk/blob/145f4d5c76770f904783e100a2017ba7b4c9a52b/android/app/src/main/java/com/headswap/desk/CarLifeSession.kt#L250)

Протокольные проверки и локальные испытания подтверждают только перечисленные сценарии. Совместимость с каждой версией официального приложения Baidu CarLife и каждым Wi-Fi Direct ГУ требует отдельных испытаний.
