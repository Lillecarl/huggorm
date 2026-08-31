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

#include <exception>
#include <string>

#include "nix/store/store-api.hh"
#include "nix/store/store-dir-config.hh"
#include "nix/util/error.hh"
#include "nix/util/terminal.hh"

namespace huggorm {

/**
 * One exception class, by module and name.
 *
 * The module is a PARAMETER. It used to be the literal
 * "huggorm_bindings.errors" here, which was a fourth copy of a name
 * the build already derives three ways - `errors_module()` from the
 * declaration's stem, `_policy.ERROR_MODULE`, and the emitted file
 * name. Renaming the declaration left this one behind, and the only
 * symptom was every nix error quietly arriving as a RuntimeError
 * through the fallback below (tasks/063).
 *
 * So the emitted catch chain passes both strings and this file names
 * no part of the library it raises into.
 *
 * Imported on the first failure rather than at load time, so nothing
 * here runs while the package is still importing itself. No static
 * cache: one would hold whichever module asked first, which is a
 * wrong answer waiting for a second error module. After the first
 * import this is a sys.modules lookup, on a path that is already
 * building an exception.
 *
 * Returns a new reference, or null with a Python error already set.
 */
inline PyObject * error_class(const char * module, const char * name)
{
    PyObject * mod = PyImport_ImportModule(module);
    if (mod == nullptr) {
        return nullptr;
    }
    PyObject * cls = PyObject_GetAttrString(mod, name);
    Py_DECREF(mod);
    return cls;
}

/**
 * Raise `module.name`, carrying the message both ways: plain first,
 * then exactly what libstore wrote.
 */
inline void raise_as(const char * module, const char * name,
                     const std::exception & e)
{
    // libstore writes its messages in colour whether or not anything
    // is a terminal, so what() holds "\x1b[31;1merror:\x1b[0m" and
    // friends. Escape codes are wrong in a traceback and wrong over
    // the wire, so str(e) is the stripped one - but the colour exists
    // so an error can be PRINTED to a terminal, and throwing it away
    // here would take that from every caller who has one.
    const std::string colored = e.what();
    const std::string plain = nix::filterANSIEscapes(colored, /*filterAll=*/true);

    PyObject * cls = error_class(module, name);
    if (cls == nullptr) {
        // The message still beats losing it. Something is very wrong
        // with the package if this happens.
        PyErr_Clear();
        PyErr_SetString(PyExc_RuntimeError, plain.c_str());
        return;
    }
    PyObject * exc = PyObject_CallFunction(cls, "ss", plain.c_str(), colored.c_str());
    if (exc == nullptr) {
        Py_DECREF(cls);
        return;  // constructing the exception failed; that error stands
    }
    PyErr_SetObject(cls, exc);
    Py_DECREF(exc);
    Py_DECREF(cls);
}


}  // namespace huggorm
