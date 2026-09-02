#!/usr/bin/env Rscript
# plot_spotting.R -- Prism-style figures from spotting_quant output.
#
# Reads <stub>_normalized.csv, runs a PAIRED t-test of every strain against the
# WT control, and draws a scatter-dot plot with mean +/- SD and significance
# brackets, styled to match GraphPad Prism via ggprism.
#
# The test is paired because each replicate contributes one control spot and one
# spot per strain, measured on the SAME plate, in the SAME dilution row, under
# the same lighting and background subtraction. Those shared nuisance factors
# cancel within a replicate, so pairing on `replicate` is both correct and much
# more powerful at n = 4 than an unpaired test would be.
#
# Usage:
#   Rscript plot_spotting.R <normalized.csv> [options]
#
#   --outdir DIR      where to write figures + stats   (default: alongside input)
#   --control NAME    control strain                   (default: strain_col == 1)
#   --value COL       relative_growth | raw_growth     (default: relative_growth)
#   --ylab TEXT       y-axis label                     (default: "Relative Growth")
#   --width N         figure width in inches           (default: 5.2)
#   --height N        figure height in inches          (default: 5.4)
#   --p-adjust METHOD holm | BH | none                 (default: none)
#   --show-ns         also draw brackets for non-significant comparisons
#   --keep-artifacts  do NOT drop ROIs flagged as touching rim/label/bolt
#   --keep-outliers   do NOT drop single points flagged as dominating the spread
#
# Install once:
#   install.packages(c("ggplot2", "ggprism", "dplyr", "tidyr", "readr"))

suppressPackageStartupMessages({
  library(ggplot2); library(ggprism)
  library(dplyr);   library(tidyr); library(readr)
})

# ---------------------------------------------------------------- arguments --
args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 1) stop("Usage: Rscript plot_spotting.R <normalized.csv> [options]")

opt <- function(flag, default = NULL) {
  i <- match(flag, args)
  if (is.na(i) || i == length(args)) default else args[i + 1]
}
has_flag <- function(flag) flag %in% args

csv_path   <- args[1]
outdir     <- opt("--outdir", dirname(normalizePath(csv_path)))
control_in <- opt("--control", NA)
value_col  <- opt("--value", "relative_growth")
ylab_txt   <- opt("--ylab", "Relative Growth")
fig_w      <- as.numeric(opt("--width", 5.2))
fig_h      <- as.numeric(opt("--height", 5.4))
base_sz    <- as.numeric(opt("--base-size", 12))
# Asterisks are set separately: ggplot text size is in mm, so this is not on the
# same scale as base_size (pt), and Prism draws the stars noticeably larger than
# the axis text.
star_sz    <- as.numeric(opt("--star-size", 8))
# Default: NO multiplicity correction. This matches how these experiments are
# analysed by hand in GraphPad Prism -- individual ratio paired t-tests, each
# its own analysis, reported uncorrected. Prism does offer Holm-Sidak inside
# ANOVA, but it is not its default and not what the manual workflow produces.
# Verified against a hand-quantified Set 4 GLU: uncorrected p = 0.0019 (dSKN7)
# and 0.0394 (dMXR2) give ** and *, exactly the manual figure; under Holm they
# became 0.013 and 0.236, i.e. * and ns.
# Both p and p_adj are always written to spotting_paired_ttests.csv, so the
# corrected values remain available: --p-adjust holm | BH re-enables them.
p_adjust   <- opt("--p-adjust", "none")
show_ns    <- has_flag("--show-ns")
keep_art   <- has_flag("--keep-artifacts")

dir.create(outdir, showWarnings = FALSE, recursive = TRUE)

# --------------------------------------------------------------------- data --
# Python writes UTF-8 with a BOM so Excel keeps the delta glyphs; readr strips it.
dat <- readr::read_csv(csv_path, show_col_types = FALSE)

need <- c("treatment", "replicate", "strain", "strain_col", value_col)
missing <- setdiff(need, names(dat))
if (length(missing)) stop("Input is missing column(s): ", paste(missing, collapse = ", "))

n0 <- nrow(dat)
# Spots whose ROI overlaps the plate rim / label / a bolt are not measurements of
# growth. spotting_quant keeps them in this file precisely so the decision to
# exclude them is visible here rather than buried upstream.
if (!keep_art && "artifact" %in% names(dat)) {
  dat <- dplyr::filter(dat, !as.logical(artifact))
}
# Strains the operator excluded for this experiment (e.g. a WT that cannot
# respire on K-OAc). They are present in the file on purpose, with their raw
# measurements intact, but they are not part of the comparison.
# Single replicate points that dominate a strain's spread. Flagged in Python
# (sq.flag_outliers), kept in the CSV so the exclusion stays visible, dropped
# here so the figure and the stats agree with the summary table.
if (!has_flag("--keep-outliers") && "outlier" %in% names(dat)) {
  n_out <- sum(as.logical(dat$outlier), na.rm = TRUE)
  if (n_out > 0) message("Dropping ", n_out, " flagged outlier point(s).")
  dat <- dplyr::filter(dat, !as.logical(outlier))
}

if ("excluded" %in% names(dat)) {
  dat <- dplyr::filter(dat, !as.logical(excluded))
}
# Rows whose control did not grow cannot be normalised; their ratios are garbage.
if ("control_ok" %in% names(dat)) {
  dat <- dplyr::filter(dat, as.logical(control_ok))
}
if (nrow(dat) < n0) message("Dropped ", n0 - nrow(dat), " flagged/unnormalisable spot(s).")

dat <- dat %>%
  dplyr::rename(value = dplyr::all_of(value_col)) %>%
  dplyr::filter(!is.na(value))

# Strain order is decided PER EXPERIMENT, not globally (see make_plot).
#
# A single global ordering is both wrong and impossible here. Each set is its own
# strain panel, so the same name sits in different columns in different sets --
# in this corpus "WT BY" is column 1 in sets 1-4 but column 5 in sets 7-8, and
# "ΔGTR1" is column 3 in set 5 but column 7 in set 1. Taking distinct
# (strain, strain_col) pairs across everything therefore yields 58 rows for 56
# names, and factor() rejects the duplicated levels outright.
dat$strain <- as.character(dat$strain)

# -------------------------------------------------------------------- stats --
#' Ratio paired t-test: is strain/control different from 1?
#'
#' The pairing is already inside `relative_growth`: a strain's value is its own
#' spot divided by the control spot from the SAME replicate, same plate, same
#' dilution row. Testing those ratios against 1 is therefore the paired
#' comparison, expressed on the ratio scale -- what GraphPad calls a ratio
#' paired t-test, and the right form when the effect is multiplicative.
#'
#' What this replaced, and why it was wrong: a paired t-test of the strain's
#' relative values against the CONTROL COLUMN's relative values. Those two are
#' not the same quantity -- the strain's is divided by its own replicate's
#' control, the control column's by the MEAN of the controls -- so the control's
#' entire between-replicate spread was injected into every comparison. On
#' Set 2 K-OAc the raw controls ran 1.84-4.59, making the control's relative
#' values swing 0.54-1.34, and ΔGRX5 (values -0.028, -0.036, 0.056, 0.024, i.e.
#' no growth at all and extremely consistent) came out p=0.0096 -> 0.067 after
#' correction, "not significant". As a ratio test it is p=2.4e-5.
#'
#' No log transform: blank strains give ratios at or below zero (ΔGRX5 above),
#' which log cannot take.
# Ratio paired t-test, done ON THE LOGS -- which is what a "ratio paired t-test"
# means (and what GraphPad, whose look these figures copy, actually computes).
# Testing the raw ratios against 1 is wrong for multiplicative data: the spread
# grows with the mean, so a large effect penalises itself. dRHO5 on Set 6 GLY
# (ratios 7.4, 8.0, 14.7, 14.3) came out p_adj 0.100 on the raw scale and 0.007
# on logs.
#
# Values are first censored at the DETECTION LIMIT. A ratio below
# (noise floor / control) is not a small number, it is an unmeasurable one: a
# replicate reading 0.000 becomes log(-Inf) and blows up the variance of a strain
# that plainly does not grow. dPOS5 (0.063, 0.036, 0.000, 0.018) goes from
# p_adj 0.138 uncensored to 0.000 censored, which matches what the plate shows.
DETECT_GRAY <- 0.4          # matches sq.MIN_CONTROL_GRAY

ratio_tests <- function(d, control) {
  others <- setdiff(levels(d$strain), control)

  rows <- lapply(others, function(s) {
    sub <- d %>% dplyr::filter(strain == s)
    v <- sub$value
    keep <- is.finite(v)
    v <- v[keep]
    # censor at the detection limit where the control value is known
    if ("control_raw" %in% names(sub)) {
      cr <- suppressWarnings(as.numeric(sub$control_raw))[keep]
      lim <- DETECT_GRAY / cr
      v <- ifelse(is.finite(lim) & v < lim, lim, v)
    }
    n_ok <- length(v)
    if (n_ok < 2 || any(!is.finite(v)) || any(v <= 0)) {
      return(data.frame(group1 = control, group2 = s, n = n_ok,
                        mean_ratio = if (n_ok) exp(mean(log(pmax(v, 1e-9)))) else NA_real_,
                        p = NA_real_))
    }
    tt <- stats::t.test(log(v), mu = 0)
    data.frame(group1 = control, group2 = s, n = n_ok,
               mean_ratio = exp(mean(log(v))),   # geometric mean, matching the test
               p = unname(tt$p.value))
  })
  out <- do.call(rbind, rows)
  # Seven comparisons against one control is a family; not correcting inflates
  # the false-positive rate to ~30%. Both columns are reported so the choice is
  # explicit rather than implied by whichever number got plotted.
  out$p_adj <- if (p_adjust == "none") out$p else stats::p.adjust(out$p, method = p_adjust)
  out
}

# Prism's asterisk convention.
stars <- function(p) {
  ifelse(is.na(p), "",
  ifelse(p < 1e-4, "****",
  ifelse(p < 1e-3, "***",
  ifelse(p < 1e-2, "**",
  ifelse(p < 0.05, "*", "ns")))))
}

# --------------------------------------------------------------------- plot --
msd <- function(x) {
  x <- x[is.finite(x)]
  data.frame(y = mean(x), ymin = mean(x) - stats::sd(x), ymax = mean(x) + stats::sd(x))
}

#' Order one experiment's strains: control first, then the rest in plate-column
#' order. Levels are built from THIS experiment only, so a name reused at a
#' different column in another set cannot collide.
order_strains <- function(d, control) {
  lv <- d %>%
    dplyr::distinct(strain, strain_col) %>%
    dplyr::arrange(strain_col) %>%
    dplyr::pull(strain) %>%
    as.character() %>%
    unique()
  # Prism convention: the control sits at the far left and every bracket runs
  # rightwards from it. A control left in the middle of the panel would make the
  # brackets fan out in both directions and collide.
  if (control %in% lv) lv <- c(control, setdiff(lv, control))
  factor(as.character(d$strain), levels = lv)
}

make_plot <- function(d, res, control, title = NULL) {

  p_used <- if (p_adjust == "none") res$p else res$p_adj
  res$label <- stars(p_used)
  keep <- if (show_ns) rep(TRUE, nrow(res)) else res$label != "ns" & res$label != ""
  brackets <- res[keep, , drop = FALSE]

  top <- max(c(d$value, msd(d$value)$ymax), na.rm = TRUE)
  # Round the axis up to a clean 0.5 step, as Prism does.
  y_top <- ceiling(top / 0.5) * 0.5
  breaks <- seq(0, y_top, by = 0.5)
  # A blank strain sits just below zero after background subtraction, and
  # clamping the panel at 0 would silently delete those points. Drop the floor
  # just far enough to show them; the ticks (and, with prism_offset, the drawn
  # axis line) still start at 0, so the figure reads exactly like the Prism one.
  low <- min(c(d$value, msd(d$value)$ymin), na.rm = TRUE)
  y_bot <- if (low < 0) low - 0.04 * y_top else 0

  # Brackets live ABOVE the panel, in the margin, so the y-axis can stop at the
  # top tick (as Prism does) while still reaching the panel floor and meeting
  # the x-axis. Keeping them inside would force the axis to run up past the
  # last tick to enclose them.
  has_title <- !is.null(title) && nzchar(title)
  n_br <- 0
  step_frac <- 0
  if (nrow(brackets)) {
    # Stack in x-order, nearest comparison lowest, so they never cross.
    brackets <- brackets[order(match(brackets$group2, levels(d$strain))), ]
    n_br <- nrow(brackets)
    # Spacing shrinks once there are many brackets so the stack stays within a
    # bounded slice of the panel instead of growing without limit.
    step_frac <- min(0.10, 0.45 / n_br)
    brackets$y.position <- y_top + (y_top - y_bot) * step_frac * seq_len(n_br)
  }

  # Reserve the top space in the SAME geometry the brackets are drawn in.
  # Bracket positions are DATA units (a fraction of the y-range above the top
  # tick) but the reservation is in points, so the two only agree if the panel
  # height is taken into account. Reserving a flat "points per bracket" broke on
  # figures with FEW brackets: less was reserved, so the panel was taller, so one
  # step spanned more points than had been set aside and the bar landed on the
  # title (seen on Set 8 GLU with 1 bracket and Set 5 GLY with 2, while the
  # 5- and 6-bracket figures were fine).
  panel_pt <- fig_h * 72 * 0.80          # panel is ~80% of figure height here
  bracket_pad <- if (n_br) step_frac * n_br * panel_pt + star_sz * 2.845 + 8
                 else 0

  g <- ggplot(d, aes(x = strain, y = value)) +
    # SD whiskers first, so the points sit on top of them.
    stat_summary(fun.data = msd, geom = "errorbar",
                 width = 0.28, linewidth = 0.55) +
    # Mean as a plain wide rule (errorbar with zero height beats crossbar,
    # which would draw a box outline Prism does not use).
    stat_summary(fun = mean, geom = "errorbar",
                 aes(ymax = after_stat(y), ymin = after_stat(y)),
                 width = 0.52, linewidth = 0.75) +
    geom_point(position = position_jitter(width = 0.085, height = 0, seed = 1),
               size = 1.9, shape = 16, colour = "black") +
    scale_y_continuous(breaks = breaks, expand = c(0, 0)) +
    # coord_cartesian ZOOMS rather than filtering, so nothing is dropped, and
    # clip = "off" lets the brackets render above the panel. The panel now ends
    # exactly at the top tick, so the default (untruncated) axis lines run the
    # full panel edge and meet at the bottom-left corner.
    coord_cartesian(ylim = c(y_bot, y_top), clip = "off") +
    labs(x = NULL, y = ylab_txt, title = title) +
    theme_prism(base_size = base_sz) +
    theme(
      # Prism draws strain names italic and angled; hjust/vjust keep the right
      # end of each label under its own tick instead of drifting left.
      axis.text.x  = element_text(angle = 45, hjust = 1, vjust = 1,
                                  face = "italic", size = base_sz),
      axis.text.y  = element_text(size = base_sz),
      axis.title.y = element_text(face = "bold", size = base_sz * 1.15),
      axis.line    = element_line(colour = "black", linewidth = 0.7),
      axis.ticks   = element_line(colour = "black", linewidth = 0.7),
      axis.ticks.length = unit(4, "pt"),
      legend.position = "none",
      # The brackets sit ABOVE the panel, in the same band the title occupies,
      # so the room for them goes under the title when there is one and into the
      # plot margin when there is not. Putting it in both double-spaces the top;
      # putting it in neither has the title land on top of the brackets.
      plot.title = element_text(size = base_sz * 1.1, hjust = 0.5,
                                face = "bold",
                                margin = margin(b = bracket_pad)),
      plot.margin = margin(t = if (has_title) 10 else 10 + bracket_pad,
                           r = 14, b = 6, l = 6)
    )

  if (nrow(brackets)) {
    g <- g + add_pvalue(brackets, xmin = "group1", xmax = "group2",
                        label = "label", y.position = "y.position",
                        tip.length = 0.012, label.size = star_sz,
                        bracket.size = 0.6)
  }
  g
}

# --------------------------------------------------------------------- drive --
safe <- function(s) gsub("[^A-Za-z0-9._-]+", "_", s)
all_stats <- list()

for (tr in unique(dat$treatment)) {
  d <- dplyr::filter(dat, treatment == tr)

  # Which strain is the control, in order of authority:
  #   1. --control on the command line
  #   2. the `is_control` column, i.e. the choice actually made while looking
  #      at the plates -- the control is NOT always the left-most strain, and
  #      guessing from column order silently normalizes to the wrong strain
  #   3. the lowest strain_col, for older files with no is_control column
  control <- if (!is.na(control_in)) {
    control_in
  } else if ("is_control" %in% names(d) && any(as.logical(d$is_control))) {
    ctl <- unique(as.character(d$strain[as.logical(d$is_control)]))
    if (length(ctl) > 1)
      warning("Treatment '", tr, "' marks ", length(ctl),
              " different control strains; using the first (", ctl[1], ").")
    ctl[1]
  } else {
    d %>% dplyr::filter(strain_col == min(strain_col)) %>%
      dplyr::pull(strain) %>% .[1] %>% as.character()
  }
  if (!control %in% unique(as.character(d$strain))) {
    warning("Control '", control, "' not present in treatment '", tr, "'; skipping.")
    next
  }

  # Fix the strain ordering for this experiment before anything consumes it, so
  # the table and the figure cannot disagree about which strain is which.
  d$strain <- order_strains(d, control)

  res <- ratio_tests(d, control)
  res$treatment <- tr
  all_stats[[tr]] <- res

  ttl <- if (length(unique(dat$treatment)) > 1) tr else NULL
  g <- make_plot(d, res, control, title = ttl)

  stem <- file.path(outdir, paste0("spotting_", safe(tr)))
  ggsave(paste0(stem, ".png"), g, width = fig_w, height = fig_h, dpi = 400, bg = "white")
  ggsave(paste0(stem, ".pdf"), g, width = fig_w, height = fig_h, device = cairo_pdf)
  message("Wrote ", stem, ".png / .pdf")
}

stats_df <- do.call(rbind, all_stats)
if (!is.null(stats_df)) {
  stats_df <- stats_df[, c("treatment", "group1", "group2", "n",
                           "mean_ratio", "p", "p_adj")]
  stats_path <- file.path(outdir, "spotting_paired_ttests.csv")
  readr::write_excel_csv(stats_df, stats_path)
  message("Wrote ", stats_path)
  print(stats_df, row.names = FALSE, digits = 4)
}
