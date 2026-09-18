import logging
import plotly.graph_objects as go
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from datetime import date

logger = logging.getLogger(__name__)

def plot_ratings_history(
    history_data: Dict[str, List[Tuple[date, int]]],
    title: str,
    output_filename: str,
    rating_scale: Dict[str, int],
    output_dir: str
):
    """
    Создает и сохраняет интерактивный график истории рейтингов.

    Args:
        history_data: Словарь, где ключи - названия облигаций, 
                      а значения - списки кортежей (дата, рейтинг).
        title: Заголовок графика.
        output_filename: Путь для сохранения HTML-файла.
        rating_scale: Шкала для отображения меток на оси Y.
        output_dir: Директория для сохранения файла.
    """
    if not history_data:
        logger.warning(f"Нет данных для построения графика '{title}'.")
        return

    # Преобразуем имя файла в объект Path для корректной работы с путями
    output_path = Path(output_dir) / output_filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Подготовка графика кредитных рейтингов: {title}")

    fig = go.Figure()

    # Инвертируем шкалу для меток оси Y (текст: значение)
    y_tick_labels = {v: k for k, v in rating_scale.items() if v >= 0}

    for bond_name, data_points in history_data.items():
        if not data_points:
            continue
        
        # Сортируем точки по дате
        data_points.sort(key=lambda x: x[0])
        
        dates = [dp[0] for dp in data_points]
        scores = [dp[1] for dp in data_points]

        fig.add_trace(go.Scatter(
            x=dates,
            y=scores,
            mode='lines+markers',
            name=bond_name,
            hovertemplate=
                f'<b>{bond_name}</b><br>' +
                'Дата: %{x|%d.%m.%Y}<br>' +
                'Рейтинг: %{customdata}<extra></extra>',
            customdata=[y_tick_labels.get(score, "N/A") for score in scores]
        ))

    fig.update_layout(
        title=title,
        xaxis_title="Дата",
        yaxis_title="Кредитный рейтинг",
        legend_title="Облигации",
        yaxis=dict(
            tickmode='array',
            tickvals=sorted(list(y_tick_labels.keys())),
            ticktext=[y_tick_labels[v] for v in sorted(list(y_tick_labels.keys()))]
        ),
        template="plotly_white"
    )

    fig.write_html(str(output_path))
    logger.info(f"График '{title}' сохранен в: {output_path}")

def plot_risk_history(
    history_data: Dict[str, List[Tuple[date, int]]],
    title: str,
    output_filename: str,
    output_dir: str
):
    """
    Создает и сохраняет интерактивный график истории уровня риска от Тинькофф.
    """
    if not history_data:
        logger.warning(f"Нет данных для построения графика '{title}'.")
        return

    # Преобразуем имя файла в объект Path
    output_path = Path(output_dir) / output_filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Подготовка графика уровней риска: {title}")

    fig = go.Figure()

    # Метки для оси Y (0: Не определен, 1: Низкий, 2: Средний, 3: Высокий)
    y_tick_labels = {
        0: "N/A",
        1: "Низкий",
        2: "Средний",
        3: "Высокий"
    }

    for bond_name, data_points in history_data.items():
        if not data_points:
            continue
        
        data_points.sort(key=lambda x: x[0])
        dates = [dp[0] for dp in data_points]
        scores = [dp[1] for dp in data_points]

        fig.add_trace(go.Scatter(
            x=dates,
            y=scores,
            mode='lines+markers',
            name=bond_name,
            hovertemplate=
                f'<b>{bond_name}</b><br>' +
                'Дата: %{x|%d.%m.%Y}<br>' +
                'Уровень риска: %{customdata}<extra></extra>',
            customdata=[y_tick_labels.get(score, "N/A") for score in scores]
        ))

    fig.update_layout(
        title=title,
        xaxis_title="Дата",
        yaxis_title="Уровень риска (Тинькофф)",
        legend_title="Облигации",
        yaxis=dict(
            tickmode='array',
            tickvals=sorted(list(y_tick_labels.keys())),
            ticktext=[y_tick_labels[v] for v in sorted(list(y_tick_labels.keys()))]
        ),
        template="plotly_white"
    )

    fig.write_html(str(output_path))
    logger.info(f"График '{title}' сохранен в: {output_path}")

def filter_rating_changes(history_data: Dict[str, List[Tuple[date, int]]], direction: str) -> Dict[str, List[Tuple[date, int]]]:
    """
    Фильтрует историю, оставляя только бумаги с ростом или падением рейтинга.
    """
    filtered_history = {}
    for bond_name, data_points in history_data.items():
        if len(data_points) < 2:
            continue
            
        data_points.sort(key=lambda x: x[0])
        last_rating = data_points[-1][1]
        prev_rating = data_points[-2][1]

        if direction == 'up' and last_rating > prev_rating:
            filtered_history[bond_name] = data_points
        elif direction == 'down' and last_rating < prev_rating:
            filtered_history[bond_name] = data_points
            
    return filtered_history 

def plot_listlevel_history(
    history_data: Dict[str, List[Tuple[date, int]]],
    title: str,
    output_filename: str,
    output_dir: str
):
    """
    Создает и сохраняет интерактивный график истории уровней листинга MOEX.
    """
    if not history_data:
        logger.warning(f"Нет данных для построения графика '{title}'.")
        return

    output_path = Path(output_dir) / output_filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Подготовка графика уровней листинга: {title}")

    fig = go.Figure()

    y_tick_labels = {1: "1 (высший)", 2: "2 (средний)", 3: "3 (базовый)"}

    for bond_name, data_points in history_data.items():
        if not data_points:
            continue
        
        data_points.sort(key=lambda x: x[0])
        dates = [dp[0] for dp in data_points]
        scores = [dp[1] for dp in data_points]

        fig.add_trace(go.Scatter(
            x=dates,
            y=scores,
            mode='lines+markers',
            name=bond_name,
            hovertemplate=
                f'<b>{bond_name}</b><br>' +
                'Дата: %{x|%d.%m.%Y}<br>' +
                'Уровень листинга: %{y}<extra></extra>'
        ))

    fig.update_layout(
        title=title,
        xaxis_title="Дата",
        yaxis_title="Уровень листинга (1 - лучший)",
        legend_title="Облигации",
        yaxis=dict(
            tickmode='array',
            tickvals=sorted(list(y_tick_labels.keys())),
            ticktext=[y_tick_labels.get(v, str(v)) for v in sorted(list(y_tick_labels.keys()))],
            autorange='reversed' # Инвертируем ось, чтобы 1 был вверху
        ),
        template="plotly_white"
    )

    fig.write_html(str(output_path))
    logger.info(f"График '{title}' сохранен в: {output_path}")

def plot_liquidity_history(
    history_data: Dict[str, List[Tuple[date, float]]],
    title: str,
    output_filename: str,
    output_dir: str
):
    """
    Создает и сохраняет интерактивный график истории ликвидности облигаций.
    
    Args:
        history_data: Словарь, где ключи - ISIN облигаций, 
                      а значения - списки кортежей (дата, коэффициент потерь в %).
        title: Заголовок графика.
        output_filename: Путь для сохранения HTML-файла.
        output_dir: Директория для сохранения файла.
    """
    if not history_data:
        logger.warning(f"Нет данных для построения графика '{title}'.")
        return

    output_path = Path(output_dir) / output_filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Подготовка графика ликвидности: {title}")

    fig = go.Figure()

    for bond_isin, data_points in history_data.items():
        if not data_points:
            continue
        
        data_points.sort(key=lambda x: x[0])
        dates = [dp[0] for dp in data_points]
        loss_ratios = [dp[1] for dp in data_points]

        # Определяем цвет линии на основе последнего значения риска ликвидности
        latest_loss = loss_ratios[-1] if loss_ratios else 0
        if latest_loss >= 50:
            color = 'red'      # Критический риск
        elif latest_loss >= 20:
            color = 'orange'   # Высокий риск  
        elif latest_loss >= 10:
            color = 'gold'     # Умеренный риск
        else:
            color = 'green'    # Низкий риск

        fig.add_trace(go.Scatter(
            x=dates,
            y=loss_ratios,
            mode='lines+markers',
            name=bond_isin,
            line=dict(color=color),
            hovertemplate=
                f'<b>{bond_isin}</b><br>' +
                'Дата: %{x|%d.%m.%Y}<br>' +
                'Потери при выходе: %{y:.2f}%<extra></extra>'
        ))

    # Добавляем горизонтальные линии для пороговых значений
    fig.add_hline(y=10, line_dash="dash", line_color="gold", 
                  annotation_text="10% - Умеренный риск", annotation_position="top right")
    fig.add_hline(y=20, line_dash="dash", line_color="orange",
                  annotation_text="20% - Высокий риск", annotation_position="top right")
    fig.add_hline(y=50, line_dash="dash", line_color="red",
                  annotation_text="50% - Критический риск", annotation_position="top right")

    fig.update_layout(
        title=title,
        xaxis_title="Дата",
        yaxis_title="Потери при выходе по рынку (%)",
        legend_title="Облигации (ISIN)",
        yaxis=dict(
            range=[0, max(100, max([max(data_points, key=lambda x: x[1])[1] 
                                  for data_points in history_data.values() if data_points], 
                                  default=[0, 0]) * 1.1)],
            ticksuffix="%"
        ),
        template="plotly_white"
    )

    fig.write_html(str(output_path))
    logger.info(f"График '{title}' сохранен в: {output_path}") 