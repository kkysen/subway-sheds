import re

from subway_sheds import paths


def test_readme_shows_the_generic_embed_as_it_is() -> None:
    # `README.md` shows `embed/queenslink/generic.partial.html` but for its comment,
    # indented in a list.
    embed = (paths.ROOT / "embed" / "queenslink" / "generic.partial.html").read_text()
    embed = re.sub(r"<!--.*?-->\n", "", embed, flags=re.DOTALL)
    readme = (paths.ROOT / "README.md").read_text()
    shown = re.search(r"\n  ```html\n(.*?)  ```\n", readme, flags=re.DOTALL)
    assert shown is not None
    assert re.sub(r"^  ", "", shown.group(1), flags=re.MULTILINE) == embed
