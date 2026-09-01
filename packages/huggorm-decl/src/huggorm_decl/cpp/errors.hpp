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
// The catches are ordered most-derived first, because a base class
// catch would swallow its subclasses.

#include <Python.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <exception>
#include <string>
#include <utility>

#include "nix/store/store-api.hh"
#include "nix/store/store-dir-config.hh"
#include "nix/util/error.hh"
#include "nix/util/terminal.hh"

namespace huggorm {

/**
 * `module.name` as a live Python exception, built and NOT raised.
 *
 * Two callers want different halves of the same work. The translator
 * below wants the exception RAISED. A binding that answers with one -
 * a BuildResult carries a nix::BuildError as a value, and reading a
 * failed result is not an exception (tasks/071) - wants the OBJECT.
 * So the object is what this makes, and raising it is one line.
 *
 * The module is a PARAMETER. It used to be the literal
 * "huggorm_bindings.errors" here, which was a fourth copy of a name
 * the build already derives three ways - `errors_module()` from the
 * declaration's stem, `_policy.ERROR_MODULE`, and the emitted file
 * name. Renaming the declaration left this one behind, and the only
 * symptom was every nix error quietly arriving as a RuntimeError
 * through the fallback below (tasks/063).
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
    return cls(plain, colored, std::forward<Extra>(extra)...);
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
 */
inline void raise_as(const char * module, const char * name,
                     const std::exception & e)
{
    try {
        nanobind::object exc = as_error(module, name, e);
        PyErr_SetObject(exc.type().ptr(), exc.ptr());
    } catch (...) {
        PyErr_Clear();
        PyErr_SetString(
            PyExc_RuntimeError,
            nix::filterANSIEscapes(e.what(), /*filterAll=*/true).c_str());
    }
}


}  // namespace huggorm
