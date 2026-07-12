import io
from datetime import datetime, timedelta
from itertools import combinations
from zoneinfo import ZoneInfo

import altair as alt
import httpx
import polars as pl
import streamlit as st
from streamlit.delta_generator import DeltaGenerator
from streamlit_javascript import st_javascript

from common.actions import get_stats, get_user_animes
from common.filesystem import anime_db_path
from common.franchises import get_user_franchises
from common.user_list import UserList, UserNotFoundError
from common.utils import set_page_config

set_page_config(
    layout="wide",
)

st.title("List Analysis")

base_width = 600
base_height = 300

# Get user name
col1, _ = st.columns([1, 4])
user_name = st.session_state.get("user_name", "")
user_name = col1.text_input("Your MAL username:", user_name)
st.session_state["user_name"] = user_name

# Get user infos
user_infos = st_javascript(
    "[new Date().toISOString(), Intl.DateTimeFormat().resolvedOptions().timeZone, navigator.languages]",
    "user_time_detector",
)
if isinstance(user_infos, list):
    user_iso_time, user_tz_name, user_langs = user_infos
else:
    # Fake data
    user_iso_time, user_tz_name, user_langs = "2020-01-01", "Asia/Tokyo", ["en-US"]
user_time = datetime.fromisoformat(user_iso_time).astimezone(ZoneInfo(user_tz_name))

# st.write(f"User time: {user_time}")
# st.write(f"User lang: {user_langs}")


@st.cache_data(show_spinner=False)
def analyse(user_name: str, user_time: datetime, user_langs: list[str]):
    with httpx.Client(
        timeout=httpx.Timeout(30),
    ) as client:
        user_list = UserList.from_user_name(client, user_name)
    user_animes = get_user_animes(user_list, anime_db_path, user_langs)
    user_franchises = get_user_franchises(user_animes)
    stats = get_stats(user_animes, user_franchises, user_time)
    return stats, user_franchises, user_animes


if st.button("Launch analysis"):
    if not user_name:
        st.error("Please provide your MyAnimeList username")
        st.stop()

    with st.spinner("Working..."):
        try:
            stats, user_franchises, user_animes = analyse(
                user_name,
                user_time,
                user_langs,
            )
            user_animes: pl.DataFrame  # type annotation
        except UserNotFoundError:
            st.error(f"User '{user_name}' not found (your list might be private)")
            st.stop()

    st.write("## Download your data")
    st.write("Download your data to analyse it offline")
    buffer = io.BytesIO()
    user_animes.write_parquet(buffer)
    st.download_button(
        label="Download data",
        data=buffer.getvalue(),
        file_name=f"{user_name}.parquet",
    )

    st.write("## Current air schedule")
    st.write(f"Times are in {str(user_time.tzinfo)} timezone")
    st.dataframe(
        stats["air_schedule"],
        hide_index=True,
        use_container_width=True,
    )

    st.write("## Next releases")
    st.write("What to look forward to?")
    st.dataframe(
        stats["next_releases"],
        hide_index=True,
        width=base_width,
    )

    # st.write("## Favourite franchises")
    # st.write("What do you like the most?")
    # st.dataframe(
    # 	stats["favorite_franchises"],
    # 	hide_index=True,
    # )

    # Compute all watched episodes
    watched_duration: timedelta = (
        user_animes.filter(pl.col("user_watch_episodes").is_not_null())
        .select(pl.col("user_watch_episodes") * pl.col("episode_avg_duration"))
        .sum()
        .item()
    )
    to_watch_duration: timedelta = (
        user_animes.select(
            pl.max_horizontal(pl.col("episodes"), pl.col("user_watch_episodes"))
            * pl.col("episode_avg_duration")
        )
        .sum()
        .item()
    )

    st.write("## Watched episodes")
    st.write(
        f"You have watched {watched_duration.total_seconds() / 3600:.1f} hours of anime out of {to_watch_duration.total_seconds() / 3600:.1f} hours (planned to watch + unfinished)"
    )
    st.write(
        f"You have watched {watched_duration / to_watch_duration:.1%} of the anime you want to watch"
    )

    col1, col2 = st.columns(2)

    col1.write("## Franchises score distribution")
    col1.write("How do you score anime compared to the MAL users?")
    col1.altair_chart(
        alt.Chart(
            user_animes.filter(
                pl.col("scored_avg").is_not_null() & pl.col("user_scored").is_not_null()
            )
        )
        .transform_fold(["scored_avg", "user_scored"], as_=["variable", "value"])
        .transform_density(
            density="value",
            groupby=["variable"],
            as_=["value", "density"],
            extent=[0, 10],
        )
        .mark_line()
        .encode(
            x=alt.X("value:Q", title="Score"),
            y=alt.Y("density:Q", title="Density"),
            color=alt.Color(
                "variable:N",
                legend=alt.Legend(
                    title=None,
                    labelExpr="datum.label == 'scored_avg' ? 'MyAnimeList Score' : 'User Score'",
                ),
            ),
        )
        .properties(width=base_width, height=base_height)
        .interactive()
    )

    col2.write("## User score distribution by air year")
    col2.write("Are you biased towards newer animes?")

    points = (
        alt.Chart(
            user_animes.filter(
                pl.col("user_scored").is_not_null() & pl.col("air_start").is_not_null()
            )
        )
        .mark_point()
        .encode(
            x=alt.X("air_start:T", title="Air Year"),
            y=alt.Y("user_scored:Q", title="Score", scale=alt.Scale(domain=(0, 10))),
            tooltip=[
                alt.Tooltip("title:N", title="Title"),
                alt.Tooltip("user_scored:Q", title="Score"),
                alt.Tooltip("air_start:T", title="Air Start"),
            ],
        )
        .properties(width=base_width, height=base_height)
        .interactive()
    )

    tendency_line = points.transform_regression(
        "air_start",
        "user_scored",
        method="linear",
    ).mark_line(color="red", opacity=0.8)

    col2.altair_chart(points + tendency_line)

    def score_box_plot(key: str, col: DeltaGenerator):
        threshold = 8
        box_data = (
            user_animes.filter(
                pl.col("user_scored").is_not_null()
                & pl.col(key).is_not_null()
            )
            .select("user_scored", key)
            .explode(key)
            .group_by(key)
            .all()
            .filter(pl.col("user_scored").list.len() >= threshold)
            .cast(
                {
                    # Removes filtered keys from the plot
                    key: pl.String
                }
            )
            .with_columns(mean_score=pl.col("user_scored").list.mean())
            .explode("user_scored")
            .sort("mean_score", key, descending=True)
        )

        col.altair_chart(
            alt.Chart(box_data)
            .mark_boxplot()
            .encode(
                x=alt.X(
                    key, title=key.capitalize(), sort=box_data.get_column(key).to_list()
                ),
                y=alt.Y(
                    "user_scored:Q", title="Score", scale=alt.Scale(domain=(0, 10))
                ),
            )
            .properties(
                title=key.capitalize(), width=base_width, height=base_height + 150
            )
        )

    st.write("## User score distribution by genres, themes, studios and demographics")
    st.write("What variables influence your scoring?")
    col1, col2 = st.columns(2)
    score_box_plot("genres", col1)
    score_box_plot("themes", col2)
    score_box_plot("studios", col1)
    score_box_plot("demographics", col2)

    # Scale scores to remove bias in MAL users scoring and user scoring
    def scale_scores(col: pl.Series) -> pl.Series:
        "Scale scores to a range of 0 to 1 using rank scaling."
        return 1 - (col.rank(descending=True) - 1) / (col.count() - 1)

    unpopular_data = (
        user_animes.filter(
            pl.col("scored_avg").is_not_null() & pl.col("user_scored").is_not_null()
        )
        .with_columns(
            user_scored_scaled=scale_scores(pl.col("user_scored")),
            scored_avg_scaled=scale_scores(pl.col("scored_avg")),
        )
        .with_columns(
            score_difference=pl.col("user_scored_scaled") - pl.col("scored_avg_scaled")
        )
        .with_columns(score_difference_abs=pl.col("score_difference").abs())
        .sort("score_difference_abs", descending=True)
    )

    # TODO option to compute mal popularity from mal scores or mal members (select box)
    col1, col2 = st.columns(2)
    col1.write("## Most unpopular opinions")
    col1.write("Do you have any hot takes?")
    col1.dataframe(
        unpopular_data.select(
            "title", "score_difference", "scored_avg", "user_scored"
        ).rename(
            {
                "title": "Title",
                "score_difference": "Normed Score Diff (%)",
                "scored_avg": "MyAnimeList Score",
                "user_scored": "User Score",
            }
        ),
        hide_index=True,
        width=base_width,
        height=base_width,
    )

    normie_ness = 1 - (unpopular_data.get_column("score_difference_abs").mean() * 2)
    st.write("## Normie-ness")
    st.write("Higly scientific metric to measure how normie you are")
    st.write(f"Normie-ness: {normie_ness:.2%}")

    unpopular_data_colored = unpopular_data.with_columns(
        color=pl.when(pl.col("score_difference_abs") <= 0.05)
        .then(pl.lit("green"))
        .when(pl.col("score_difference_abs") <= 0.15)
        .then(pl.lit("orange"))
        .otherwise(pl.lit("red"))
    )

    col2.write("## User score vs MyAnimeList score")
    col2.write(
        "Do you agree with the general public? Or are you going against the flow?"
    )
    points = (
        alt.Chart(unpopular_data_colored)
        .mark_circle()
        .encode(
            x=alt.X("scored_avg_scaled:Q", title="MyAnimeList Score"),
            y=alt.Y("user_scored_scaled:Q", title="User Score"),
            color=alt.Color("color:N", scale=None),
            tooltip=[
                alt.Tooltip("title:N", title="Title"),
                alt.Tooltip("scored_avg:Q", title="MyAnimeList Score"),
                alt.Tooltip("user_scored:Q", title="User Score"),
                alt.Tooltip("score_difference:Q", title="Normed Score Diff (%)"),
            ],
        )
        .properties(width=base_width, height=base_width)
        .interactive()
    )

    # TODO Debug
    # tendency_line = points.transform_regression(
    #     "scored_avg_scaled",
    #     "user_scored_scaled",
    #     method="linear",
    # ).mark_line(color="blue", opacity=0.8)

    # col2.altair_chart(points + tendency_line)
    col2.altair_chart(points)

    def co_occurrence(data: pl.Series) -> pl.DataFrame:
        "Compute co-occurrence data from a list of lists."

        co_occurrences = []
        for row in data:
            if row is None or len(row) < 2:
                continue
            for feature1, feature2 in combinations(sorted(row), 2):
                co_occurrences.append(
                    {
                        "feature1": feature1,
                        "feature2": feature2,
                    }
                )

        if not co_occurrences:
            return pl.DataFrame({"feature1": [], "feature2": [], "count": []})

        df = pl.DataFrame(
            co_occurrences,
        )

        # Count co-occurrences
        return (
            df.group_by(["feature1", "feature2"])
            .agg(pl.len().alias("count"))
            .sort("count", descending=True)
        )

    def draw_co_occurrence(feature: str, col: DeltaGenerator):
        "Draw a co-occurrence matrix with a title and masks the upper triangle."
        filtered = user_animes.filter(pl.col(feature).is_not_null())
        if filtered.height == 0:
            col.info(f"No {feature} data available")
            return
        occ_data = co_occurrence(
            filtered.get_column(feature)
        )

        # TODO format data into a matrix with lower triangle masked
        col.altair_chart(
            alt.Chart(occ_data)
            .mark_rect()
            .encode(
                x=alt.X("feature1:N", title="Feature 1"),
                y=alt.Y("feature2:N", title="Feature 2"),
                color=alt.Color("count:Q", title="Count"),
                tooltip=[
                    alt.Tooltip("count:Q", title="Count"),
                    alt.Tooltip("feature1:N", title="Feature 1"),
                    alt.Tooltip("feature2:N", title="Feature 2"),
                ],
            )
            .properties(title=feature.capitalize(), width=base_width, height=base_width)
        )

    st.write("## Co-occurrence charts")
    st.write("What genres and themes combinations do you watch the most?")

    col1, col2 = st.columns(2)
    draw_co_occurrence("genres", col1)
    draw_co_occurrence("themes", col2)
