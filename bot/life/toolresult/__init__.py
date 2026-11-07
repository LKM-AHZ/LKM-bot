"""Tool-result handling for the Life layer (S6a).

Oversized tool results are condensed before they enter the model history; the
raw text is stashed per session so the ``read_tool_result`` tool can bring back
whatever the summary dropped.
"""
