# PLAYBOOK — как повторить поиск авиамаршрутов через Travelpayouts / Aviasales API

Документ для передачи любому разработчику. Цель: за час получить рабочий доступ к тем же данным,
которыми пользуется Aviasales, и повторить методику поиска «самый быстрый / самый дешёвый / со стоповером
в новой стране». Всё ниже проверено на боевом API в сентябре 2026 года; цифры из реального кейса
Екатеринбург → Джакарта.

Содержание:

1. [Что это и что даёт](#1-что-это-и-что-даёт)
2. [Регистрация и токен](#2-регистрация-и-токен)
3. [Три слоя API Travelpayouts](#3-три-слоя-api-travelpayouts)
4. [GraphQL Flights Data API — главный инструмент](#4-graphql-flights-data-api--главный-инструмент)
5. [REST Data API — что полезно](#5-rest-data-api--что-полезно)
6. [Запасной канал без токена](#6-запасной-канал-без-токена)
7. [Тупики — куда не ходить](#7-тупики--куда-не-ходить)
8. [Инструменты этого репозитория](#8-инструменты-этого-репозитория)
9. [Методика поиска маршрута](#9-методика-поиска-маршрута)
10. [Кейс: Екатеринбург → Джакарта](#10-кейс-екатеринбург--джакарта)
11. [Чеклист воспроизведения](#11-чеклист-воспроизведения)

---

## 1. Что это и что даёт

Travelpayouts — партнёрская платформа Aviasales. Партнёрам бесплатно открыт **Data API**: кэш цен,
который собирает поисковик Aviasales по запросам реальных пользователей. Кэш обновляется постоянно,
цены живые (найдены за последние ~48 часов), покрытие по СНГ и Азии отличное.

Что реально получается из этого API:

- цена, авиакомпания, номера рейсов, время вылета и прилёта каждого сегмента в локальном времени;
- пересадки: аэропорт, страна, длительность, флаги «ночная» и «нужна транзитная виза»;
- агентство (gate) и deep-link на конкретный билет в Aviasales;
- десятки предложений на одну дату по одному направлению (через GraphQL).

Чего **не** получается: живого поиска «здесь и сейчас» по всем авиакомпаниям. Это отдельный
Flight Search API, доступ к нему дают только проектам с 50 000+ активных пользователей в месяц.
Для личного планирования и аналитики кэша достаточно, что и показал кейс ниже.

## 2. Регистрация и токен

1. https://www.travelpayouts.com → Sign up. Нужна только почта, KYC и верификации нет.
2. В кабинете подключить программу Aviasales (одна кнопка). Это условие доступа к GraphQL.
3. Tools → API → скопировать **токен**: 32 символа hex.
4. Токен передаётся:
   - в REST — query-параметром `token=...`;
   - в GraphQL — заголовком `X-Access-Token: ...`.
5. Хранить в переменной окружения `TRAVELPAYOUTS_TOKEN`, в репозиторий не коммитить
   (`.env` в `.gitignore`). Если токен попал в чат или лог — перевыпустить в кабинете.

Где официальная документация:

| Что | Ссылка |
|---|---|
| Категория «API и данные» в базе знаний | https://support.travelpayouts.com/hc/ru/categories/200358578 |
| Статья про GraphQL Flights Data API | https://support.travelpayouts.com/hc/en-us/articles/4417975783314 |
| GraphQL playground со схемой (нужен токен в заголовке) | http://api.travelpayouts.com/graphql/v1/playground |
| Справочник REST Data API v1/v2/v3 | https://travelpayouts.github.io/slate/ |
| Flight Search API (live, новая версия с 1.11.2025) | https://support.travelpayouts.com/hc/en-us/articles/30565016140434 |
| Требования к доступу к Flight Search API | https://support.travelpayouts.com/hc/en-us/articles/210995808 |

Нюанс: страницы `support.travelpayouts.com` отдают 403 не-браузерным клиентам. Та же статья
читается через публичный API Zendesk, если нужно скачать документацию скриптом:

```bash
curl -s https://support.travelpayouts.com/api/v2/help_center/en-us/articles/4417975783314.json \
  | python3 -c "import json,sys,re,html; b=json.load(sys.stdin)['article']['body']; print(html.unescape(re.sub('<[^>]+>',' ',b)))"
```

## 3. Три слоя API Travelpayouts

| Слой | Эндпоинты | Что отдаёт | Доступ |
|---|---|---|---|
| **REST Data API** | `api.travelpayouts.com/aviasales/v3/...`, `/v1/prices/...`, `/v2/prices/...` | Кэш. **Одно** самое дешёвое предложение на дату. Авиакомпания и число пересадок есть, номеров рейсов и времени стыковок нет | Токен |
| **GraphQL Data API** | `POST api.travelpayouts.com/graphql/v1/query` | Тот же кэш, но **все** предложения на дату (до сотен) с полными сегментами, пересадками и deep-link | Тот же токен |
| **Flight Search API** | `POST tickets-api.travelpayouts.com/search/affiliate/start` + polling результатов | Живой поиск по всем агентствам, как на сайте Aviasales | Только проектам с 50 000+ MAU, подпись запросов, marker |

Вывод: для аналитики маршрутов основной инструмент — GraphQL. REST удобен для календарей цен
и быстрых проверок «есть ли вообще что-то на эту дату».

## 4. GraphQL Flights Data API — главный инструмент

### 4.1. Запрос

```bash
curl -s -X POST https://api.travelpayouts.com/graphql/v1/query \
  -H "X-Access-Token: $TRAVELPAYOUTS_TOKEN" -H "Content-Type: application/json" \
  -d @- <<'EOF'
{"query": "{ prices_one_way(
    params: { origin: \"SVX\", destination: \"CGK\", depart_dates: [\"2026-09-09\",\"2026-09-10\"] },
    paging: { limit: 200, offset: 0 }, sorting: VALUE_ASC, grouping: NONE,
    currency: \"usd\", market: \"ru\"
  ) {
    departure_at value currency duration number_of_changes gate main_airline with_baggage ticket_link
    segments {
      transfers { at to country_code duration_seconds night_transfer visa_required }
      flight_legs { origin destination departure_at arrival_at flight_number operating_carrier aircraft_code }
    }
  } }"}
EOF
```

### 4.2. Параметры `params` (тип `ParamsOneWay`)

| Параметр | Смысл |
|---|---|
| `origin`, `destination` | IATA аэропорта, города или страны. `MOW` = все аэропорты Москвы |
| `depart_dates: ["YYYY-MM-DD", ...]` | Конкретные даты. **Максимум 5 за запрос** (иначе 400 `length of depart_dates exceeds allowable maximum of 5`) |
| `depart_date_min`, `depart_date_max` | Диапазон дат, удобен для второго плеча стоповера |
| `depart_months: ["YYYY-MM-01"]` | Целые месяцы |
| `direct: true` | Только прямые |
| `convenient: true` | Не более одной пересадки и без ночных стыковок |
| `with_baggage: true` | Только с багажом |
| `no_visa_at_transfer: true` | Без транзитной визы |
| `value_min`, `value_max` | Фильтр по цене |
| `trip_class` | Класс |

Аргументы запроса вне `params`: `paging {limit, offset}`, `sorting` (`VALUE_ASC`, `ROUTE_WEIGHT_DESC`,
`DISTANCE_UNIT_PRICE_ASC`, `DISCOUNT_DESC`), `grouping` (`NONE`, `DEPART_DATE`, `DATES_NUM_OF_CHANGES`,
`YEAR_MONTH`, ...), `currency` (ISO, нижний регистр), `market` (`ru`, `kz`, `us` — влияет на набор
агентств и цены).

Есть также `prices_round_trip`, `special_offers_one_way/round_trip`, `weekend_prices_one_way` с той же логикой.

### 4.3. Поля ответа (тип `Price`)

`value` (float, в валюте запроса), `currency`, `departure_at`, `duration` (минуты чистого полёта),
`number_of_changes`, `gate` (агентство), `main_airline`, `with_baggage`, `found_at`, `ticket_link`,
`segments[]`:

- `segments[].flight_legs[]`: `origin`, `destination`, `departure_at`, `arrival_at` (ISO с оффсетом
  таймзоны, например `2026-09-10T15:20:00+05:00`), `flight_number`, `operating_carrier`, `aircraft_code`;
- `segments[].transfers[]`: `at`, `to` (аэропорты), `country_code`, `duration_seconds`, `night_transfer`,
  `visa_required`.

Deep-link на билет: `https://www.aviasales.ru/search` + `ticket_link`. Внутри `t=` зашит маршрут
(`SVXMCXDWC`), это удобно для быстрого чтения без разбора сегментов.

### 4.4. Как узнать схему самому

Интроспекция работает с тем же токеном:

```bash
curl -s -X POST https://api.travelpayouts.com/graphql/v1/query -H "X-Access-Token: $TRAVELPAYOUTS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"{ __type(name:\"ParamsOneWay\") { inputFields { name description type { name ofType { name } } } } }"}'
```

Подставляйте `Price`, `Segment`, `Transfer`, `FlightLeg`, `Sorting`, `GroupingOneWay` — этого хватает
для любой задачи. Тот же результат в удобном виде даёт вкладка Docs в playground.

### 4.5. Ограничения, найденные эмпирически

- **5 дат** в `depart_dates`. Режьте на чанки или используйте диапазон.
- **Rate limit жёсткий.** Два параллельных потока уже дают `429 Too Many Requests`. Рабочая схема:
  один поток, retry с backoff 2 → 4 → 8 → 16 → 32 с. 34 направления обрабатываются за ~60–75 с.
- Один и тот же рейс приходит несколько раз от разных агентств с разницей в $1–5. Дедуплицируйте
  по ключу «рейсы + время вылета», оставляя минимальную цену.
- Цена — float (`49.37`), сравнивайте с допуском.
- Это кэш: если направление никто не искал, его не будет. Ноль результатов не значит «рейсов нет».

## 5. REST Data API — что полезно

Все запросы `GET`, база `https://api.travelpayouts.com`, везде `&token=...`.

| Эндпоинт | Зачем | Пример |
|---|---|---|
| `/aviasales/v3/prices_for_dates` | Самое дешёвое предложение на дату: цена, `airline`, `flight_number`, `transfers`, `duration`, `link`. С `direct=true` — самый дешёвый прямой | `?origin=SVX&destination=CGK&departure_at=2026-09-10&one_way=true&currency=usd&sorting=price&limit=30` |
| `/v2/prices/month-matrix` | Календарь месяца по дням | `?origin=SVX&destination=CGK&month=2026-09-01&currency=usd&show_to_affiliates=false` |
| `/v1/prices/cheap` | Самое дешёвое с 0 / 1 / 2 пересадками на дату | `?origin=SVX&destination=DXB&depart_date=2026-09-10&currency=usd` |
| `/v1/prices/direct` | Прямые рейсы по месяцу | `?origin=SVX&destination=DXB&depart_date=2026-09&currency=usd` |
| `/v2/prices/latest` | Найденное за 48 часов, с `number_of_changes` и `duration` | `?origin=SVX&destination=CGK&one_way=true&period_type=month&beginning_of_period=2026-09-01` |
| `/data/en/airports.json`, `/data/en/airlines.json` | Справочники без токена | — |

Нюансы REST:

- `limit` в `prices_for_dates` не даёт больше одного предложения на дату. Для полноты — GraphQL.
- `sorting=time` возвращает 400. Работают `price` и `route`.
- В ответах one-way иногда есть `return_at`: кэш смешивает тарифы туда-обратно, цену проверяйте по ссылке.
- Коды: Улан-Батор `UBN`, не `ULN`. Бишкек `FRU` API не знает (400 `unknown location code`).
- `link` из `prices_for_dates` содержит маршрут и `expected_price`, открывается как deep-link Aviasales.

## 6. Запасной канал без токена

У фронтенда Aviasales есть открытый сервис календаря цен `lyssa.aviasales.ru`. Он неофициальный,
может измениться без предупреждения, но на момент проверки работает без авторизации и отдаёт
тот же кэш минимальных цен в валюте рынка (для `market=ru` — рубли):

```bash
# минимальная цена на дату / по месяцу
curl -s "https://lyssa.aviasales.ru/date_picker_prices?origin_iata=SVX&destination_iata=CGK&one_way=true&market=ru&depart_date=2026-09-09"
curl -s "https://lyssa.aviasales.ru/date_picker_prices?origin_iata=SVX&destination_iata=CGK&one_way=true&market=ru&depart_month=2026-09-01"

# найденное за последние дни с числом пересадок и длительностью
curl -s "https://lyssa.aviasales.ru/latest_prices?origin=SVX&destination=CGK&one_way=true&period_type=month&beginning_of_period=2026-09-01"
```

Годится для первого прицела «на какие даты и через какие хабы вообще есть цены», пока регистрируете
токен. Авиакомпаний и номеров рейсов там нет.

## 7. Тупики — куда не ходить

Проверено и не работает или не окупается:

- **Живой поиск Aviasales с фронтенда** (`tickets-api.aviasales.ru/search/...`): CloudFront отвечает 403
  или 204, нужны подпись и клиентские заголовки. Официальный путь — Flight Search API с 50 000+ MAU.
- **Другие пути `lyssa.aviasales.ru`** (`price_matrix`, `map`, `cheapest_tickets`, `calendar`, ...) — 404.
- **Google Flights** без браузера не отдаёт цены; Playwright работает, но это отдельная и хрупкая задача.
- **SEO-страницы Kayak / Trip.com** — чувствительны к слагу URL, часто 404 или капча.
- **Официальный Flight Search API v1** (`/v1/flight_search`) отключён 15.06.2026, отвечает 401.
- **Duffel** — живые цены, но KYC через Stripe, недоступный юрлицам РФ. Amadeus Self-Service закрыт.

## 8. Инструменты этого репозитория

Установка:

```bash
git clone https://github.com/Victory-SergD/SergD_AviaTickets.git && cd SergD_AviaTickets
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # единственная зависимость: httpx
export TRAVELPAYOUTS_TOKEN=<32 hex>
```

| Скрипт | Слой API | Что делает |
|---|---|---|
| `graphql_search.py search` | GraphQL | Все маршруты A→B на даты с сегментами; `--sort price\|duration`, `--max-price`, `--max-hours`, `--direct`, `--links`, `--json` |
| `graphql_search.py stopover` | GraphQL | Связка A→HUB + HUB→B по 34 хабам с проверкой стыковки по реальному времени; `--min-stop`, `--max-stop`, `--min-connect-airport-change`, `--exclude-countries`, `--max-price`, `--max-hours`, `--min-daylight`, `--sort price\|duration\|daylight` |
| `travelpayouts_search.py` | REST v3 | Самое дешёвое по направлению, веер по городам РФ, простой стоповер без проверки времени |
| `weekend_search.py` | REST | Уикенды, discover, международные направления, длинные поездки с гибкими датами |
| `duffel_search.py` | Duffel | Референс для тех, у кого есть зарубежное юрлицо |

Три команды, которые закрыли кейс из раздела 10:

```bash
# 1. что вообще есть сквозным билетом, быстрое сверху
python graphql_search.py search --from SVX --to CGK --dates 2026-09-08,2026-09-09,2026-09-10 --sort duration --max-price 1000

# 2. самопересадки со стыковкой 2.5–10 ч, быстрое сверху
python graphql_search.py stopover --from SVX --to CGK --dates 2026-09-08,2026-09-09,2026-09-10 \
    --min-stop 2.5 --max-stop 10 --max-price 1000 --sort duration

# 3. сутки-двое в новой стране, дешёвое сверху, страны, где уже были, исключены
python graphql_search.py stopover --from SVX --to CGK --dates 2026-09-08,2026-09-09,2026-09-10 \
    --min-stop 16 --max-stop 50 --max-price 850 --max-hours 60 --exclude-countries CN MN UZ TH MY SG VN KG TM TR RU

# 4. только те стоянки, которые реально можно потратить на город (8+ часов светлого времени)
python graphql_search.py stopover --from SVX --to CGK --dates 2026-09-08,2026-09-09,2026-09-10 \
    --min-stop 7 --max-stop 30 --min-daylight 8 --max-price 1000 --sort duration
```

## 8a. Что выяснилось на практике

Факты, проверенные на боевых данных. Экономят часы при следующем поиске.

### Стоянка: длительность не равна пользе

`--min-daylight` считает, сколько часов стоянки попадает в местные 09:00–22:00. Разница принципиальная:

| Стоянка | Часов | Светлых | Что это на деле |
|---|---|---|---|
| Абу-Даби, прилёт 19:30 → вылет 09:20 | 13ч50м | 2ч20м | ночь в отеле |
| Абу-Даби, прилёт 06:40 → вылет 20:55 | 14ч15м | 11ч50м | полный день в городе |
| Дубай, прилёт 11:10 → вылет 22:20 | 11ч10м | 10ч50м | день и вечер |

Фильтровать надо по светлому времени, не по общей длительности.

### Прямые рейсы бывают не каждый день

Аэрофлот SU626 Екатеринбург → Пхукет летает раз в неделю. На 10 сентября он есть за $822, на 8 и 9 сентября его нет вообще, и маршрут сразу дорожает и удлиняется. Всегда прогонять минимум 3 даты: разница в цене между соседними днями доходит до двух раз.

### Скорость и новая страна конфликтуют

Пять самых быстрых маршрутов SVX → Джакарта (19,6–22,8 ч) шли через Таиланд, Малайзию, Китай и Узбекистан, все со стыковками 3–6 часов. Первый вариант с полноценным днём в новом городе оказался на 8 часов длиннее и на $160 дороже. Это структурное свойство направления, а не случайность выборки.

### Проверка аэропорта перед поиском

```bash
curl -s https://api.travelpayouts.com/data/en/airports.json \
  | python3 -c "import json,sys; [print(a['code'],a['name'],'city=',a['city_code']) for a in json.load(sys.stdin) if a.get('code')=='KJT']"
# KJT Kertajati International Airport city= BDO
```

`city_code` показывает, к какому городу привязан аэропорт: KJT относится к Бандунгу, не к Джакарте. Если по коду 0 предложений из всех хабов, аэропорт международным трафиком не обслуживается, каким бы новым он ни был.

### Форс-мажор ломает связки из двух билетов

При извержении Анак Кракатау 4–6 сентября 2026 закрывали 8 аэропортов Явы и Бали, задето 2300 рейсов. Правило: пока действует уровень тревоги, связка из двух билетов теряет смысл. Отмена первого плеча не компенсирует второе. Единый билет дороже на $100–200, но перевозчик обязан пересадить.

Уровень тревоги: https://magma.esdm.go.id, статус аэропортов ищется по названию аэропорта плюс «status».

### Требования въезда влияют на посадку

Индонезия выдаёт визу по прибытии ($35, 30 дней) только при наличии билета на выезд. Без него не пускают на посадку в вылетающем аэропорту. Дешёвый билет на выезд ищется тем же скриптом: Джакарта → Куала-Лумпур $55, Джакарта → Сингапур $63.

### Данные, которые спросят при бронировании

Из загранпаспорта РФ: фамилия и имя латиницей ровно как в машиночитаемой строке (отчество не вводится), пол, дата рождения, гражданство, номер паспорта без пробела, даты выдачи и окончания. Паспорт должен быть действителен ещё 6 месяцев после въезда.

### Чего API не делает

Не покупает. Travelpayouts отдаёт цены и deep-link, покупка идёт на сайте агентства вручную. Автоматизация оплаты не предусмотрена ни в одном слое API.

---

## 9. Методика поиска маршрута

1. **Календарь.** `month-matrix` или lyssa по паре A→B: понять, какие даты дешевле и есть ли данные вообще.
2. **Сквозные билеты.** GraphQL `search` по нужным датам, две сортировки: по цене и по времени в пути.
   Смотреть на пересадки: ночные, со сменой аэропорта (`VKO → SVO`), с транзитной визой.
3. **Связки через хабы.** GraphQL `stopover` в трёх режимах:
   транзит (стыковка 2.5–10 ч) — ищет самопересадки, которых нет в сквозных билетах;
   стоповер (16–50 ч) — день в новой стране, страны отсекать `--exclude-countries`;
   полезный стоповер — `--min-daylight 8`, отсекает ночные стоянки.
4. **Проверка стыковок.** Скрипт считает по реальному времени с таймзонами. Для разных аэропортов
   одного города (DWC/DXB, VKO/SVO, DMK/BKK, KUL/SZB) закладывать 4+ часа. Раздельные билеты — риск
   на пассажире: при опоздании первого второй сгорает.
5. **Живая цена.** Открыть deep-link (`--links`). Кэш обычно точен до $10–30, за 3–5 дней до вылета
   цены растут ежедневно.
6. **Визы и правила въезда.** Проверять страну хаба, если планируется выход из аэропорта, и страну
   назначения: обратный или дальнейший билет, виза по прибытии, сроки подачи e-visa.
7. **Форс-мажор.** Вулканы, забастовки, погода. При активном предупреждении брать единый билет
   вместо связки: раздельные билеты при отмене не компенсируются.
8. **Сравнение.** Сквозной билет против связки: в кейсе связка была дешевле сквозного на те же
   самолёты и без переезда между аэропортами Москвы.

## 10. Кейс: Екатеринбург → Джакарта

Условия: вылет 8–10 сентября 2026, бюджет $850 (быстро) или $1000 (максимально быстро),
сутки-двое допустимы в новой стране, посещённые страны — Китай, Монголия, Узбекистан, Таиланд,
Малайзия, Сингапур, Вьетнам, Турция. Курс ЦБ на дату поиска 86,59 ₽/$.

Что дал кэш (все цифры — вывод скриптов, deep-links проверялись):

| Задача | Маршрут | Цена | В пути |
|---|---|---|---|
| Дёшево и разумно быстро | 10.09 Победа SVX 05:30 → SVO, Etihad EY842/EY472 SVO 11:20 → AUH → CGK 11.09 21:00 | $492–498 | 37,5 ч |
| Быстро в бюджет $850 | 08.09 15:20 SVX → Махачкала → Дубай DWC (Ural + Победа), Emirates EK358 DXB 10:50 → CGK 22:25 | $582 | 29 ч |
| Максимально быстро до $1000 | 09.09 flydubai FZ8346 SVX 00:05 → DXB 05:20, Emirates EK358 10:50 → CGK 22:25 | $781 | 20 ч 20 м |
| Стоповер, ОАЭ | Как выше, Emirates на день позже | $583 | 29 ч в Дубае |
| Стоповер, Казахстан | Red Wings WZ1073 08.09 SVX → ALA, Air China CA456/CA475 09.09 → Чэнду → CGK | $575 | 44 ч в Алматы |
| Стоповер, Индия | Победа + Аэрофлот SVX → VKO/SVO → DEL, IndiGo DEL → BOM → CGK | $524 | 33 ч в Дели, нужна e-visa |
| Сквозной билет для сравнения | 10.09 Победа + Etihad SVX → VKO → SVO → AUH → CGK | $521 | 38 ч, смена аэропорта |
| Самый быстрый до $1000 | 10.09 Аэрофлот SU626 SVX → Пхукет, Batik → Куала-Лумпур, Citilink → CGK | $747 | 20,0 ч |
| Самый быстрый единым билетом | 08.09 Аэрофлот + Etihad SVX → SVO → AUH → CGK | $881 | 23,3 ч |
| Итоговый выбор: день на Пхукете | 08.09 Аэрофлот SVX → SVO → Пхукет, сутки, Scoot → Сингапур → CGK | $463 | 50,7 ч, 25 ч на острове |

Выводы, которые переносятся на любой маршрут:

- Кэш через REST показывал только «2 пересадки, 38–62 часа». Полная картина появилась только в GraphQL.
- Прямые рейсы SVX → Дубай (flydubai, Ural) в REST-кэше не были самыми дешёвыми на дату и потому
  не показывались вовсе; GraphQL их вернул.
- Связка «Победа до Москвы + один билет на дальнее плечо» стабильно дешевле сквозного составного
  билета Aviasales на те же рейсы.
- Emirates DXB → CGK держит цену $318–323 при любой дате, Etihad AUH → CGK нон-стоп стоит вдвое
  дороже, чем с пересадкой в Мумбаи; такие константы полезно знать заранее.

## 11. Чеклист воспроизведения

- [ ] Зарегистрироваться на travelpayouts.com, подключить программу Aviasales, скопировать токен.
- [ ] `export TRAVELPAYOUTS_TOKEN=...`, проверить: `curl "https://api.travelpayouts.com/aviasales/v3/prices_for_dates?origin=MOW&destination=LED&departure_at=2026-10&token=$TRAVELPAYOUTS_TOKEN"` даёт `"success":true`.
- [ ] Прогнать GraphQL-запрос из раздела 4.1 через curl, убедиться, что приходят `segments`.
- [ ] Склонировать репозиторий, `pip install -r requirements.txt`.
- [ ] `python graphql_search.py search --from <A> --to <B> --dates <даты> --sort duration`.
- [ ] `python graphql_search.py stopover ...` в режиме транзита (2.5–10 ч) и стоповера (16–50 ч).
- [ ] Открыть deep-links лучших вариантов, сверить цену с живым поиском.
- [ ] Проверить визы хаба и назначения, буфер стыковки при раздельных билетах.
- [ ] Прогнать минимум 3 даты подряд: сезонные прямые рейсы летают не каждый день.
- [ ] Для стоповера добавить `--min-daylight 8`, иначе получите ночь в отеле вместо города.
- [ ] Проверить требования въезда: билет на выезд, виза, срок действия паспорта.
- [ ] При активном форс-мажоре в стране назначения брать единый билет, не связку.
- [ ] Держать один поток запросов к GraphQL; при 429 — backoff, не параллелить.
- [ ] Токен не коммитить; если утёк — перевыпустить.
