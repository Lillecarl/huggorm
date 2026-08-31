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
 * One exception class from huggorm_bindings.errors, by name.
 *
 * Imported on the first failure rather than at load time, so nothing
 * here runs while the package is still importing itself. Returns a new
 * reference, or null with a Python error already set.
 */
inline PyObject * error_class(const char * name)
{
    static PyObject * module = PyImport_ImportModule("huggorm_bindings.errors");
    if (module == nullptr) {
        return nullptr;
    }
    return PyObject_GetAttrString(module, name);
}

/**
 * Raise `name` from huggorm_bindings.errors, carrying the message
 * both ways: plain first, then exactly what libstore wrote.
 */
inline void raise_as(const char * name, const std::exception & e)
{
    // libstore writes its messages in colour whether or not anything
    // is a terminal, so what() holds "\x1b[31;1merror:\x1b[0m" and
    // friends. Escape codes are wrong in a traceback and wrong over
    // the wire, so str(e) is the stripped one - but the colour exists
    // so an error can be PRINTED to a terminal, and throwing it away
    // here would take that from every caller who has one.
    const std::string colored = e.what();
    const std::string plain = nix::filterANSIEscapes(colored, /*filterAll=*/true);

    PyObject * cls = error_class(name);
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
