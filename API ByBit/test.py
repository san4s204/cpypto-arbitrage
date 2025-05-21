import requests
import pandas as pd
import time

def interval_to_seconds(interval):
    if interval == "1":
        return 60
    elif interval == "3":
        return 180
    elif interval == "5":
        return 300
    elif interval == "15":
        return 900
    elif interval == "30":
        return 1800
    elif interval == "60":
        return 3600
    elif interval == "120":
        return 7200
    elif interval == "240":
        return 14400
    elif interval == "360":
        return 21600
    elif interval == "720":
        return 43200
    elif interval == "D":
        return 86400
    else:
        raise ValueError("Unknown interval")

def fetch_bybit_ohlcv(symbol, interval, start_time, end_time):
    url = "https://api.bybit.com/public/linear/kline"
    all_data = []
    limit = 200  # максимальное число свечей за 1 запрос
    
    current_time = start_time
    while current_time < end_time:
        params = {
            "symbol": symbol,
            "interval": interval,
            "from": current_time,
            "limit": limit
        }
        response = requests.get(url, params=params)
        print(f"Запрос: {response.url}, Статус: {response.status_code}, Текст ответа: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError:
            print("Ошибка: не удалось распарсить JSON")
            print(f"Ответ сервера: {response.text}")
            break
        
        if data["ret_code"] != 0:
            print("Ошибка API:", data["ret_msg"])
            break
        
        result = data["result"]
        if not result:
            break
        
        all_data.extend(result)
        last_timestamp = result[-1]["open_time"]
        if last_timestamp == current_time:
            # Прекращаем если данные не двигаются вперед
            break
        current_time = last_timestamp + interval_to_seconds(interval)
        time.sleep(0.2)  # задержка, чтобы не превышать лимит запросов
    
    return all_data

def process_data_to_df(raw_data):
    df = pd.DataFrame(raw_data)
    df['open_time'] = pd.to_datetime(df['open_time'], unit='s')
    # Оставим нужные столбцы и переведем в числа
    cols = ['open_time', 'open', 'high', 'low', 'close', 'volume']
    df = df[cols]
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
    return df

if __name__ == "__main__":
    import datetime
    
    end_time = int(time.time())
    start_time = end_time - 3*30*24*3600  # 3 месяца назад примерно
    
    symbol = "BTCUSDT"
    interval = "60"  # 1 час
    
    print("Загружаем данные с Bybit...")
    raw_data = fetch_bybit_ohlcv(symbol, interval, start_time, end_time)
    print(f"Получено {len(raw_data)} записей")
    
    df = process_data_to_df(raw_data)
    
    excel_filename = "BTCUSDT_Bybit_3months.xlsx"
    df.to_excel(excel_filename, index=False)
    print(f"Данные сохранены в файл {excel_filename}")
