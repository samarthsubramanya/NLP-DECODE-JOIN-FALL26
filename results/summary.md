# Task 1 baselines

PR@15 with fixed gold labels. Heuristic JoinGR returns nothing without join edges, so on stripped and obfuscated graphs the best method is dense retrieval. Obfuscated keeps table names, so small schemas (Spider, KaggleDBQA) barely move.

| dataset | encoder | documented (best) | stripped (best) | gap | obfuscated (best) | gap |
|---|---|---|---|---|---|---|
| beaver | bge | 0.496 (heuristic) | 0.314 (vanilla) | -0.182 | 0.165 (vanilla) | -0.331 |
| beaver | minilm | 0.421 (heuristic) | 0.240 (vanilla) | -0.182 | 0.099 (vanilla) | -0.322 |
| bird | bge | 0.967 (vanilla) | 0.960 (vanilla) | -0.007 | 0.915 (vanilla) | -0.053 |
| bird | minilm | 0.958 (heuristic) | 0.933 (vanilla) | -0.025 | 0.825 (vanilla) | -0.134 |
| spider | bge | 0.999 (vanilla) | 0.999 (vanilla) | +0.000 | 0.997 (vanilla) | -0.002 |
| spider | minilm | 0.998 (vanilla) | 0.997 (vanilla) | -0.001 | 0.976 (vanilla) | -0.022 |
| kaggledbqa | bge | 1.000 (vanilla) | 1.000 (vanilla) | +0.000 | 0.996 (vanilla) | -0.004 |
| kaggledbqa | minilm | 1.000 (vanilla) | 1.000 (vanilla) | +0.000 | 0.993 (vanilla) | -0.007 |
| fiben | bge | 0.827 (heuristic) | 0.053 (vanilla) | -0.773 | 0.027 (vanilla) | -0.800 |
| fiben | minilm | 0.560 (heuristic) | 0.043 (vanilla) | -0.517 | 0.043 (vanilla) | -0.517 |

## Columns

Column R@15: columns ranked by query similarity inside each method's top-15 tables, against gold columns parsed from the gold SQL (gold/<dataset>.json).

| dataset | encoder | documented (best) | stripped (best) | gap | obfuscated (best) | gap |
|---|---|---|---|---|---|---|
| beaver | bge | 0.273 (heuristic) | 0.172 (vanilla) | -0.101 | 0.118 (vanilla) | -0.155 |
| beaver | minilm | 0.221 (heuristic) | 0.136 (vanilla) | -0.085 | 0.079 (vanilla) | -0.142 |
| bird | bge | 0.666 (vanilla) | 0.637 (vanilla) | -0.030 | 0.494 (vanilla) | -0.172 |
| bird | minilm | 0.612 (vanilla) | 0.609 (vanilla) | -0.003 | 0.445 (vanilla) | -0.167 |
| spider | bge | 0.944 (vanilla) | 0.941 (vanilla) | -0.003 | 0.805 (vanilla) | -0.140 |
| spider | minilm | 0.945 (vanilla) | 0.915 (vanilla) | -0.029 | 0.731 (vanilla) | -0.214 |
| kaggledbqa | bge | 0.917 (vanilla) | 0.872 (vanilla) | -0.045 | 0.731 (vanilla) | -0.185 |
| kaggledbqa | minilm | 0.867 (vanilla) | 0.806 (vanilla) | -0.060 | 0.659 (vanilla) | -0.208 |
| fiben | bge | 0.296 (heuristic) | 0.212 (vanilla) | -0.084 | 0.125 (vanilla) | -0.171 |
| fiben | minilm | 0.187 (heuristic) | 0.146 (vanilla) | -0.042 | 0.143 (vanilla) | -0.044 |

## beaver

| encoder | method | setting | R@5 | R@15 | PR@5 | PR@15 | PR@15 fixed gold | Δ vs documented | col R@15 | col PR@15 |
|---|---|---|---|---|---|---|---|---|---|---|
| bge | vanilla | *paper* | 0.476 | 0.697 | 0.124 | 0.339 | | | | |
| bge | vanilla | documented | 0.477 | 0.696 | 0.132 | 0.364 | 0.364 |  | 0.248 | 0.008 |
| bge | vanilla | no_desc | 0.428 | 0.676 | 0.124 | 0.331 | 0.331 | -0.033 | 0.206 | 0.017 |
| bge | vanilla | stripped | 0.428 | 0.649 | 0.124 | 0.314 | 0.314 | -0.050 | 0.172 | 0.000 |
| bge | vanilla | obfuscated | 0.393 | 0.522 | 0.074 | 0.165 | 0.165 | -0.198 | 0.118 | 0.000 |
| bge | heuristic | *paper* | 0.423 | 0.759 | 0.058 | 0.405 | | | | |
| bge | heuristic | documented | 0.486 | 0.793 | 0.107 | 0.496 | 0.496 |  | 0.273 | 0.017 |
| bge | heuristic | no_desc | 0.449 | 0.768 | 0.107 | 0.455 | 0.455 | -0.041 | 0.228 | 0.025 |
| bge | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.496 | 0.000 | 0.000 |
| bge | heuristic | obfuscated | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.496 | 0.000 | 0.000 |
| bge | trained | *paper* | 0.553 | 0.875 | 0.157 | 0.645 | | | | |
| minilm | vanilla | *paper* | 0.385 | 0.667 | 0.066 | 0.273 | | | | |
| minilm | vanilla | documented | 0.363 | 0.665 | 0.050 | 0.264 | 0.264 |  | 0.187 | 0.008 |
| minilm | vanilla | no_desc | 0.348 | 0.626 | 0.066 | 0.273 | 0.273 | +0.008 | 0.163 | 0.000 |
| minilm | vanilla | stripped | 0.330 | 0.606 | 0.058 | 0.240 | 0.240 | -0.025 | 0.136 | 0.000 |
| minilm | vanilla | obfuscated | 0.272 | 0.408 | 0.017 | 0.099 | 0.099 | -0.165 | 0.079 | 0.000 |
| minilm | heuristic | *paper* | 0.414 | 0.779 | 0.074 | 0.430 | | | | |
| minilm | heuristic | documented | 0.420 | 0.751 | 0.091 | 0.421 | 0.421 |  | 0.221 | 0.008 |
| minilm | heuristic | no_desc | 0.390 | 0.682 | 0.066 | 0.355 | 0.355 | -0.066 | 0.179 | 0.000 |
| minilm | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.421 | 0.000 | 0.000 |
| minilm | heuristic | obfuscated | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.421 | 0.000 | 0.000 |
| minilm | trained | *paper* | 0.528 | 0.873 | 0.182 | 0.653 | | | | |

## bird

| encoder | method | setting | R@5 | R@15 | PR@5 | PR@15 | PR@15 fixed gold | Δ vs documented | col R@15 | col PR@15 |
|---|---|---|---|---|---|---|---|---|---|---|
| bge | vanilla | *paper* | 0.918 | 0.983 | 0.830 | 0.964 | | | | |
| bge | vanilla | documented | 0.917 | 0.983 | 0.830 | 0.963 | 0.967 |  | 0.666 | 0.319 |
| bge | vanilla | no_desc | 0.901 | 0.978 | 0.807 | 0.957 | 0.960 | -0.007 | 0.642 | 0.289 |
| bge | vanilla | stripped | 0.904 | 0.979 | 0.811 | 0.957 | 0.960 | -0.007 | 0.637 | 0.280 |
| bge | vanilla | obfuscated | 0.828 | 0.948 | 0.694 | 0.911 | 0.915 | -0.053 | 0.494 | 0.209 |
| bge | heuristic | *paper* | 0.789 | 0.951 | 0.650 | 0.921 | | | | |
| bge | heuristic | documented | 0.910 | 0.973 | 0.840 | 0.962 | 0.967 |  | 0.659 | 0.316 |
| bge | heuristic | no_desc | 0.887 | 0.969 | 0.806 | 0.959 | 0.964 | -0.003 | 0.636 | 0.289 |
| bge | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.967 | 0.000 | 0.000 |
| bge | heuristic | obfuscated | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.967 | 0.000 | 0.000 |
| bge | trained | *paper* | 0.934 | 0.973 | 0.877 | 0.961 | | | | |
| minilm | vanilla | *paper* | 0.886 | 0.970 | 0.775 | 0.936 | | | | |
| minilm | vanilla | documented | 0.887 | 0.970 | 0.776 | 0.936 | 0.939 |  | 0.612 | 0.272 |
| minilm | vanilla | no_desc | 0.862 | 0.965 | 0.743 | 0.930 | 0.934 | -0.005 | 0.605 | 0.277 |
| minilm | vanilla | stripped | 0.861 | 0.964 | 0.743 | 0.929 | 0.933 | -0.007 | 0.609 | 0.275 |
| minilm | vanilla | obfuscated | 0.791 | 0.902 | 0.649 | 0.820 | 0.825 | -0.115 | 0.445 | 0.189 |
| minilm | heuristic | *paper* | 0.858 | 0.945 | 0.762 | 0.917 | | | | |
| minilm | heuristic | documented | 0.902 | 0.967 | 0.824 | 0.954 | 0.958 |  | 0.607 | 0.272 |
| minilm | heuristic | no_desc | 0.874 | 0.958 | 0.780 | 0.944 | 0.949 | -0.010 | 0.608 | 0.285 |
| minilm | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.958 | 0.000 | 0.000 |
| minilm | heuristic | obfuscated | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.958 | 0.000 | 0.000 |
| minilm | trained | *paper* | 0.909 | 0.966 | 0.839 | 0.951 | | | | |

## spider

| encoder | method | setting | R@5 | R@15 | PR@5 | PR@15 | PR@15 fixed gold | Δ vs documented | col R@15 | col PR@15 |
|---|---|---|---|---|---|---|---|---|---|---|
| bge | vanilla | *paper* | 0.745 | 0.752 | 0.729 | 0.741 | | | | |
| bge | vanilla | documented | 0.745 | 0.752 | 0.729 | 0.741 | 0.999 |  | 0.944 | 0.826 |
| bge | vanilla | no_desc | 0.747 | 0.752 | 0.731 | 0.741 | 0.999 | +0.000 | 0.941 | 0.837 |
| bge | vanilla | stripped | 0.749 | 0.752 | 0.735 | 0.741 | 0.999 | +0.000 | 0.941 | 0.837 |
| bge | vanilla | obfuscated | 0.696 | 0.750 | 0.664 | 0.739 | 0.997 | -0.002 | 0.805 | 0.664 |
| bge | heuristic | *paper* | 0.715 | 0.747 | 0.699 | 0.736 | | | | |
| bge | heuristic | documented | 0.715 | 0.747 | 0.699 | 0.736 | 0.958 |  | 0.920 | 0.795 |
| bge | heuristic | no_desc | 0.720 | 0.747 | 0.706 | 0.736 | 0.958 | +0.000 | 0.912 | 0.796 |
| bge | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.958 | 0.000 | 0.000 |
| bge | heuristic | obfuscated | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.958 | 0.000 | 0.000 |
| bge | trained | *paper* | 0.737 | 0.747 | 0.722 | 0.736 | | | | |
| minilm | vanilla | *paper* | 0.749 | 0.752 | 0.735 | 0.740 | | | | |
| minilm | vanilla | documented | 0.749 | 0.752 | 0.735 | 0.740 | 0.998 |  | 0.945 | 0.830 |
| minilm | vanilla | no_desc | 0.744 | 0.751 | 0.726 | 0.738 | 0.995 | -0.003 | 0.911 | 0.796 |
| minilm | vanilla | stripped | 0.742 | 0.752 | 0.723 | 0.741 | 0.997 | -0.001 | 0.915 | 0.800 |
| minilm | vanilla | obfuscated | 0.661 | 0.740 | 0.626 | 0.723 | 0.976 | -0.022 | 0.731 | 0.581 |
| minilm | heuristic | *paper* | 0.739 | 0.748 | 0.726 | 0.737 | | | | |
| minilm | heuristic | documented | 0.739 | 0.748 | 0.726 | 0.737 | 0.958 |  | 0.921 | 0.800 |
| minilm | heuristic | no_desc | 0.728 | 0.746 | 0.715 | 0.735 | 0.957 | -0.001 | 0.886 | 0.764 |
| minilm | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.958 | 0.000 | 0.000 |
| minilm | heuristic | obfuscated | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.958 | 0.000 | 0.000 |
| minilm | trained | *paper* | 0.747 | 0.748 | 0.735 | 0.737 | | | | |

## kaggledbqa

| encoder | method | setting | R@5 | R@15 | PR@5 | PR@15 | PR@15 fixed gold | Δ vs documented | col R@15 | col PR@15 |
|---|---|---|---|---|---|---|---|---|---|---|
| bge | vanilla | documented | 0.996 | 1.000 | 0.996 | 1.000 | 1.000 |  | 0.917 | 0.827 |
| bge | vanilla | no_desc | 0.993 | 1.000 | 0.993 | 1.000 | 1.000 | +0.000 | 0.869 | 0.749 |
| bge | vanilla | stripped | 0.993 | 1.000 | 0.993 | 1.000 | 1.000 | +0.000 | 0.872 | 0.756 |
| bge | vanilla | obfuscated | 0.972 | 0.996 | 0.967 | 0.996 | 0.996 | -0.004 | 0.731 | 0.653 |
| bge | heuristic | documented | 0.478 | 0.478 | 0.478 | 0.478 | 0.478 |  | 0.423 | 0.354 |
| bge | heuristic | no_desc | 0.474 | 0.474 | 0.474 | 0.474 | 0.474 | -0.004 | 0.399 | 0.321 |
| bge | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.478 | 0.000 | 0.000 |
| bge | heuristic | obfuscated | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.478 | 0.000 | 0.000 |
| minilm | vanilla | documented | 0.985 | 1.000 | 0.974 | 1.000 | 1.000 |  | 0.867 | 0.738 |
| minilm | vanilla | no_desc | 0.950 | 1.000 | 0.945 | 1.000 | 1.000 | +0.000 | 0.803 | 0.686 |
| minilm | vanilla | stripped | 0.947 | 1.000 | 0.941 | 1.000 | 1.000 | +0.000 | 0.806 | 0.686 |
| minilm | vanilla | obfuscated | 0.910 | 0.993 | 0.901 | 0.993 | 0.993 | -0.007 | 0.659 | 0.550 |
| minilm | heuristic | documented | 0.478 | 0.478 | 0.478 | 0.478 | 0.478 |  | 0.415 | 0.339 |
| minilm | heuristic | no_desc | 0.469 | 0.471 | 0.467 | 0.471 | 0.471 | -0.007 | 0.370 | 0.295 |
| minilm | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.478 | 0.000 | 0.000 |
| minilm | heuristic | obfuscated | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.478 | 0.000 | 0.000 |

## fiben

| encoder | method | setting | R@5 | R@15 | PR@5 | PR@15 | PR@15 fixed gold | Δ vs documented | col R@15 | col PR@15 |
|---|---|---|---|---|---|---|---|---|---|---|
| bge | vanilla | documented | 0.339 | 0.474 | 0.023 | 0.057 | 0.057 |  | 0.214 | 0.003 |
| bge | vanilla | no_desc | 0.339 | 0.474 | 0.023 | 0.057 | 0.057 | +0.000 | 0.214 | 0.003 |
| bge | vanilla | stripped | 0.345 | 0.479 | 0.027 | 0.053 | 0.053 | -0.003 | 0.212 | 0.000 |
| bge | vanilla | obfuscated | 0.134 | 0.267 | 0.013 | 0.027 | 0.027 | -0.030 | 0.125 | 0.000 |
| bge | heuristic | documented | 0.458 | 0.881 | 0.167 | 0.827 | 0.827 |  | 0.296 | 0.007 |
| bge | heuristic | no_desc | 0.458 | 0.881 | 0.167 | 0.827 | 0.827 | +0.000 | 0.296 | 0.007 |
| bge | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.827 | 0.000 | 0.000 |
| bge | heuristic | obfuscated | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.827 | 0.000 | 0.000 |
| minilm | vanilla | documented | 0.219 | 0.361 | 0.023 | 0.043 | 0.043 |  | 0.149 | 0.003 |
| minilm | vanilla | no_desc | 0.219 | 0.361 | 0.023 | 0.043 | 0.043 | +0.000 | 0.149 | 0.003 |
| minilm | vanilla | stripped | 0.224 | 0.362 | 0.023 | 0.043 | 0.043 | +0.000 | 0.146 | 0.003 |
| minilm | vanilla | obfuscated | 0.135 | 0.321 | 0.013 | 0.043 | 0.043 | +0.000 | 0.143 | 0.013 |
| minilm | heuristic | documented | 0.291 | 0.711 | 0.057 | 0.560 | 0.560 |  | 0.187 | 0.003 |
| minilm | heuristic | no_desc | 0.291 | 0.711 | 0.057 | 0.560 | 0.560 | +0.000 | 0.187 | 0.003 |
| minilm | heuristic | stripped | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.560 | 0.000 | 0.000 |
| minilm | heuristic | obfuscated | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | -0.560 | 0.000 | 0.000 |
