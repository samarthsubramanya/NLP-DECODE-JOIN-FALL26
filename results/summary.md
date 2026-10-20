# Task 1 baselines

## beaver

| encoder | method | setting | R@5 | R@15 | PR@5 | PR@15 | PR@15 fixed gold | Δ vs documented |
|---|---|---|---|---|---|---|---|---|
| bge | vanilla | *paper* | 0.476 | 0.697 | 0.124 | 0.339 | | |
| bge | vanilla | documented | 0.477 | 0.696 | 0.132 | 0.364 | 0.364 |  |
| bge | vanilla | no_desc | 0.428 | 0.676 | 0.124 | 0.331 | 0.331 | -0.033 |
| bge | vanilla | stripped | 0.428 | 0.649 | 0.124 | 0.314 | 0.314 | -0.050 |
| bge | heuristic | *paper* | 0.423 | 0.759 | 0.058 | 0.405 | | |
| bge | heuristic | documented | 0.486 | 0.793 | 0.107 | 0.496 | 0.496 |  |
| bge | heuristic | no_desc | 0.449 | 0.768 | 0.107 | 0.455 | 0.455 | -0.041 |
| bge | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.496 |
| bge | trained | *paper* | 0.553 | 0.875 | 0.157 | 0.645 | | |
| minilm | vanilla | *paper* | 0.385 | 0.667 | 0.066 | 0.273 | | |
| minilm | vanilla | documented | 0.363 | 0.665 | 0.050 | 0.264 | 0.264 |  |
| minilm | vanilla | no_desc | 0.348 | 0.626 | 0.066 | 0.273 | 0.273 | +0.008 |
| minilm | vanilla | stripped | 0.330 | 0.606 | 0.058 | 0.240 | 0.240 | -0.025 |
| minilm | heuristic | *paper* | 0.414 | 0.779 | 0.074 | 0.430 | | |
| minilm | heuristic | documented | 0.420 | 0.751 | 0.091 | 0.421 | 0.421 |  |
| minilm | heuristic | no_desc | 0.390 | 0.682 | 0.066 | 0.355 | 0.355 | -0.066 |
| minilm | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.421 |
| minilm | trained | *paper* | 0.528 | 0.873 | 0.182 | 0.653 | | |

## bird

| encoder | method | setting | R@5 | R@15 | PR@5 | PR@15 | PR@15 fixed gold | Δ vs documented |
|---|---|---|---|---|---|---|---|---|
| bge | vanilla | *paper* | 0.918 | 0.983 | 0.830 | 0.964 | | |
| bge | vanilla | documented | 0.917 | 0.983 | 0.830 | 0.963 | 0.967 |  |
| bge | vanilla | no_desc | 0.901 | 0.978 | 0.807 | 0.957 | 0.960 | -0.007 |
| bge | vanilla | stripped | 0.904 | 0.979 | 0.811 | 0.957 | 0.960 | -0.007 |
| bge | heuristic | *paper* | 0.789 | 0.951 | 0.650 | 0.921 | | |
| bge | heuristic | documented | 0.910 | 0.973 | 0.840 | 0.962 | 0.967 |  |
| bge | heuristic | no_desc | 0.887 | 0.969 | 0.806 | 0.959 | 0.964 | -0.003 |
| bge | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.967 |
| bge | trained | *paper* | 0.934 | 0.973 | 0.877 | 0.961 | | |
| minilm | vanilla | *paper* | 0.886 | 0.970 | 0.775 | 0.936 | | |
| minilm | vanilla | documented | 0.887 | 0.970 | 0.776 | 0.936 | 0.939 |  |
| minilm | vanilla | no_desc | 0.862 | 0.965 | 0.743 | 0.930 | 0.934 | -0.005 |
| minilm | vanilla | stripped | 0.861 | 0.964 | 0.743 | 0.929 | 0.933 | -0.007 |
| minilm | heuristic | *paper* | 0.858 | 0.945 | 0.762 | 0.917 | | |
| minilm | heuristic | documented | 0.902 | 0.967 | 0.824 | 0.954 | 0.958 |  |
| minilm | heuristic | no_desc | 0.874 | 0.958 | 0.780 | 0.944 | 0.949 | -0.010 |
| minilm | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.958 |
| minilm | trained | *paper* | 0.909 | 0.966 | 0.839 | 0.951 | | |

## spider

| encoder | method | setting | R@5 | R@15 | PR@5 | PR@15 | PR@15 fixed gold | Delta vs documented |
|---|---|---|---|---|---|---|---|---|
| bge | vanilla | *paper* | 0.745 | 0.752 | 0.729 | 0.741 | | |
| bge | vanilla | documented | 0.745 | 0.752 | 0.729 | 0.741 | 0.999 |  |
| bge | vanilla | no_desc | 0.747 | 0.752 | 0.731 | 0.741 | 0.999 | +0.000 |
| bge | vanilla | stripped | 0.749 | 0.752 | 0.735 | 0.741 | 0.999 | +0.000 |
| bge | heuristic | *paper* | 0.715 | 0.747 | 0.699 | 0.736 | | |
| bge | heuristic | documented | 0.715 | 0.747 | 0.699 | 0.736 | 0.958 |  |
| bge | heuristic | no_desc | 0.720 | 0.747 | 0.706 | 0.736 | 0.958 | +0.000 |
| bge | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.958 |
| bge | trained | *paper* | 0.737 | 0.747 | 0.722 | 0.736 | | |
| minilm | vanilla | *paper* | 0.749 | 0.752 | 0.735 | 0.740 | | |
| minilm | vanilla | documented | 0.749 | 0.752 | 0.735 | 0.740 | 0.998 |  |
| minilm | vanilla | no_desc | 0.744 | 0.751 | 0.726 | 0.738 | 0.995 | -0.003 |
| minilm | vanilla | stripped | 0.742 | 0.752 | 0.723 | 0.741 | 0.997 | -0.001 |
| minilm | heuristic | *paper* | 0.739 | 0.748 | 0.726 | 0.737 | | |
| minilm | heuristic | documented | 0.739 | 0.748 | 0.726 | 0.737 | 0.958 |  |
| minilm | heuristic | no_desc | 0.728 | 0.746 | 0.715 | 0.735 | 0.957 | -0.001 |
| minilm | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.958 |
| minilm | trained | *paper* | 0.747 | 0.748 | 0.735 | 0.737 | | |
