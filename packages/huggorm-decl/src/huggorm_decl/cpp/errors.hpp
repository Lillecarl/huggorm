#pragma once
// Turn a C++ nix exception into the right Python one.
//
// nanobind's own translator maps anything it does not recognise onto
// RuntimeError, so nix::BadStorePathName would arrive as one - the
// type gone, and the message still carrying the terminal escape codes
// libstore writes into it. `nb::register_exception_translator` is the
// documented hook for doing better: nanobind calls this from inside
// catch(...), and whatever Python error it sets is what the caller
// sees.
//
// THE TRANSLATOR IS NOT HERE. It is emitted, one `catch` per declared
// error class, ordered most-derived first so a base class does not
// swallow its subclasses. This file is the two helpers it calls.

#include <Python.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <exception>
#include <string>
#include <utility>

// `filterANSIEscapes` is the one nix name this file says, and
// `terminal.hh` is the one nix header it needs.
//
// Three more sat here until 2026-09-05, for the EMITTED files: each
// includes this header and then catches `nix::InvalidPath`,
// `nix::BadStorePathName` and their kind. That was a fact about
// generated code, stated in a hand-written helper. The emitter writes
// them now, from `header = "nix/..."` beside each `cxx` in
// `decl/errors.py` (huggorm#90).
#include "nix/util/terminal.hh"

namespace huggorm {

/**
 * A `std::string` as Python text, lossily.
 *
 * Nix messages are bytes - a builder prints whatever it likes, and
 * `throw` carries a string nobody decoded - so decoding one strictly
 * would fail the translation on the first line no decoder accepts
 * and lose the error entirely. Undecodable bytes become U+FFFD,
 * which every consumer of text - tracebacks, status details,
 * terminals - reads safely. The exact bytes stay available on the
 * info parts, which cross as bytes.
 */
inline nanobind::object lossy_str(const std::string & s)
{
    // "replace" refuses nothing, so NULL means an allocation failed.
    // A null object would crash the call it is passed to.
    PyObject * text = PyUnicode_DecodeUTF8(s.data(), (Py_ssize_t) s.size(), "replace");
    if (!text)
        throw nanobind::python_error();
    return nanobind::steal<nanobind::object>(text);
}

/**
 * `module.name` as a live Python exception, built and NOT raised.
 *
 * Two callers want different halves of the same work. The translator
 * below wants the exception RAISED. A binding that answers with one -
 * a BuildResult carries a nix::BuildError as a value, and reading a
 * failed result is not an exception (huggorm#71) - wants the OBJECT.
 * So the object is what this makes, and raising it is one line.
 *
 * The module is a PARAMETER. It used to be the literal
 * "huggorm_bindings.errors" here, which was a fourth copy of a name
 * the build already derives three ways - `errors_module()` from the
 * declaration's stem, `_policy.ERROR_MODULE`, and the emitted file
 * name. Renaming the declaration left this one behind, and the only
 * symptom was every nix error quietly arriving as a RuntimeError
 * through the fallback below (huggorm#63).
 *
 * `extra` is whatever parts the class takes beyond the two strings.
 * A BuildError takes its failure word and upstream's non-determinism
 * hedge; the emitter reads both off the declaration and passes them
 * at the call site, so this names no class and no field.
 *
 * Imported on each call rather than at load time, so nothing here
 * runs while the package is still importing itself. No static cache:
 * one would hold whichever module asked first, which is a wrong
 * answer waiting for a second error module. After the first import
 * this is a sys.modules lookup.
 *
 * The message goes both ways. libstore writes its messages in colour
 * whether or not anything is a terminal, so what() holds
 * "\x1b[31;1merror:\x1b[0m" and friends. Escape codes are wrong in a
 * traceback and wrong over the wire, so str(e) is the stripped one -
 * but the colour exists so an error can be PRINTED to a terminal, and
 * throwing it away here would take that from every caller who has one.
 */
template <typename... Extra>
inline nanobind::object as_error(const char * module, const char * name,
                                 const std::exception & e, Extra &&... extra)
{
    const std::string colored = e.what();
    const std::string plain = nix::filterANSIEscapes(colored, /*filterAll=*/true);
    nanobind::object cls = nanobind::module_::import_(module).attr(name);
    return cls(lossy_str(plain), lossy_str(colored),
               std::forward<Extra>(extra)...);
}

/**
 * Raise `module.name`, carrying the message both ways.
 *
 * The object, then PyErr_SetObject. It used to do its own
 * PyObject_GetAttrString and PyObject_CallFunction with the
 * refcounting by hand; nanobind's own API says the same thing and
 * the two halves are now one statement each.
 *
 * The fallback stays. nanobind reports a failed import or a refusing
 * constructor by THROWING, and this is called from inside the
 * translator's catch(...), where letting an exception out would lose
 * the error entirely. A RuntimeError with libstore's message still
 * beats that.
 *
 * `read` holds one reader per part beyond the two strings, and each
 * takes the caught exception. They run inside the try: a reader that
 * throws gives the fallback, and cannot escape the translator.
 */
template <typename E, typename... Read>
inline void raise_as(const char * module, const char * name,
                     const E & e, Read... read)
{
    try {
        nanobind::object exc = as_error(module, name, e, read(e)...);
        PyErr_SetObject(exc.type().ptr(), exc.ptr());
    } catch (...) {
        PyErr_Clear();
        nanobind::object fallback = lossy_str(
            nix::filterANSIEscapes(e.what(), /*filterAll=*/true));
        PyErr_SetObject(PyExc_RuntimeError, fallback.ptr());
    }
}

}  // namespace huggorm
