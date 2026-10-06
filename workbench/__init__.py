"""Spotting Quantification -- every tool in one window.

The plate designer, the experiment designer and the review window each live
in their own package and each still run on their own. This package is the
program around them: one main window with a Home page (the task manager),
documents as tabs, an Explorer of the project's files, and a status bar.

    py spotting_app.py               (or run_workbench.bat)

The tools never import this package. They build themselves into a host
(`uikit.host`); `workbench.docs.DocumentHost` is the host that puts them in a
tab. Importing this package is cheap on purpose: it loads no tool, and no
numpy, until a document of that kind is first opened.
"""
