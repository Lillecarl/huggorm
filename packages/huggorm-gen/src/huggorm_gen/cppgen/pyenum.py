"""Declaration -> a plain Python module, by transforming the tree.

The second emitter that TRANSFORMS rather than prints, and for the
same reason `pyi.py` does: the output is Python and the declaration is
already Python. A vocabulary declaration is a class of `NAME = "value"`
assignments with a docstring under each, which is a StrEnum body line
for line. So this moves the declaration's own nodes and unparses them.

What changes is small and all of it derived:

- the vocabulary decorators come off, because they said what KIND of
  declaration this is rather than anything a caller sees;
- `StrEnum` goes on as the base, which is the whole of what `@words`
  means: a member IS the string libstore parses;
- the module gains its one import and a line naming the C++ that
  parses each vocabulary, so a reader can check the list against
  upstream.

Nothing else moves. The words, their order, and every docstring are
the declaration's own nodes.

There is no C++ here and nothing to compile. That is what makes a
vocabulary worth declaring rather than binding: `Store.add_to_store`
hands the string straight to a Nix parser, so the module is a name for
what libstore already accepts and a typo fails before the call.
"""

import ast

from huggorm_dsl.read import Class, Module


def class_def(cls: Class, node: ast.ClassDef) -> ast.ClassDef:
    """One vocabulary, as the StrEnum it is.

    The declaration's own body, unchanged. `@words` said this is a
    StrEnum and the base says so; everything below it was already
    written the way Python writes an enum."""
    return ast.ClassDef(
        name=cls.name,
        bases=[ast.Name(id="StrEnum", ctx=ast.Load())],
        keywords=[],
        body=list(node.body),
        decorator_list=[],
        type_params=[],
    )


def module(mod: Module, tree: ast.Module, doc: str) -> str:
    """Every vocabulary in one declaration, as a module.

    A module docstring, one import, and the classes. The import is
    the only line here that no declaration wrote, and it is the same
    line for every vocabulary there will ever be."""
    bodies = {node.name: node for node in tree.body
              if isinstance(node, ast.ClassDef)}
    out: list[ast.stmt] = [
        ast.Expr(value=ast.Constant(value=_doc(mod, doc))),
        ast.ImportFrom(module="enum", names=[ast.alias(name="StrEnum")],
                       level=0),
    ]
    for cls in mod.classes:
        if not cls.is_words:
            continue
        out.append(class_def(cls, bodies[cls.name]))
    return reflow(ast.unparse(
        ast.fix_missing_locations(ast.Module(body=out, type_ignores=[]))))


def reflow(text: str) -> str:
    """Multi-line string statements, written as blocks again.

    `ast.unparse` renders every string on one line, escapes and all.
    For a class docstring it makes an exception; for the docstring
    under an ATTRIBUTE it does not, and a four-line explanation comes
    out as one line with `\n` in it. That is the module a reader
    opens, so it is worth undoing.

    The content is already indented for where it sits: the
    declaration wrote it inside a class body and the emitted class
    body has the same indent. So the block goes back exactly as it
    was written."""
    out = []
    for line in text.splitlines():
        body = line.strip()
        if not (body[:1] in "'\"" and body[-1:] == body[:1]):
            out.append(line)
            continue
        try:
            value = ast.literal_eval(body)
        except (ValueError, SyntaxError):
            out.append(line)
            continue
        if not isinstance(value, str):
            out.append(line)
            continue
        pad = line[:len(line) - len(line.lstrip())]
        block = value.splitlines()
        out.append(f'{pad}"""{block[0]}')
        out += [each.rstrip() for each in block[1:-1]]
        out.append(f'{block[-1]}"""' if len(block) > 1 else out.pop() + '"""')
    return "\n".join(_spaced(out))


def _spaced(lines: list[str]) -> list[str]:
    """A blank line after a word that carried prose.

    `ast.unparse` writes a class body with no blank line in it, which
    reads as a wall when every second entry is a paragraph. A word
    and its docstring are ONE thing, so the break goes after the
    docstring rather than before every word - which is also why a run
    of bare words stays together."""
    out: list[str] = []
    for line in lines:
        word = (line.startswith("    ") and " = " in line
                and line.lstrip()[:1].isupper())
        if word and out and out[-1].endswith('"""'):
            out.append("")
        out.append(line)
    return out


def _doc(mod: Module, doc: str) -> str:
    """The module's docstring, with the parsers named under it.

    Which C++ takes each vocabulary is the one fact about the module
    that no member carries, and a reader checking the list against
    upstream needs it. `@words(parsed_by=...)` says it once per
    class; this collects them."""
    named = [f"{cls.name} -> {cls.decl.parsed_by}"
             for cls in mod.classes if cls.is_words and cls.decl.parsed_by]
    lines = [doc.strip(), "", "GENERATED from the declaration - do not edit."]
    if named:
        lines += ["", "The words go to:", *[f"- {line}" for line in named]]
    return "\n".join(lines) + "\n"
