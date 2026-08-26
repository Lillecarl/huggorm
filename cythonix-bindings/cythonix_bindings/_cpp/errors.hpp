#pragma once
// Turn a C++ nix exception into the right Python one.
//
// Cython's bare `except +` maps anything it does not recognise onto
// RuntimeError, so nix::BadStorePathName arrived as a RuntimeError -
// the type gone, and the message still carrying the terminal escape
// codes libstore writes into it. `except +translate_nix_error` is the
// documented hook for doing better: Cython calls this from inside
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

namespace cythonix {

/**
 * One exception class from cythonix_bindings.errors, by name.
 *
 * Imported on the first failure rather than at load time, so nothing
 * here runs while the package is still importing itself. Returns a new
 * reference, or null with a Python error already set.
 */
inline PyObject * error_class(const char * name)
{
    static PyObject * module = PyImport_ImportModule("cythonix_bindings.errors");
    if (module == nullptr) {
        return nullptr;
    }
    return PyObject_GetAttrString(module, name);
}

/**
 * Raise `name` from cythonix_bindings.errors, carrying the message
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

inline void translate_nix_error()
{
    try {
        throw;
    } catch (const nix::BadStorePathName & e) {
        raise_as("BadStorePathName", e);
    } catch (const nix::BadStorePath & e) {
        raise_as("BadStorePath", e);
    } catch (const nix::UsageError & e) {
        raise_as("UsageError", e);
    } catch (const nix::SystemError & e) {
        // The wider class, so nix::SysError and nix::WinError land
        // here too rather than falling through to NixError.
        raise_as("SysError", e);
    } catch (const nix::InvalidPath & e) {
        // The store does not hold that path. A fact about the store,
        // like Unsupported, rather than a malformed argument.
        raise_as("InvalidPath", e);
    } catch (const nix::Unsupported & e) {
        // A statement about the store, not about the call. Straight
        // off nix::Error, so it sits beside SystemError rather than
        // under it.
        raise_as("Unsupported", e);
    } catch (const nix::Error & e) {
        raise_as("NixError", e);
    }
    // Nothing follows, and the silence is the point.
    //
    // Anything else is not Nix's, and this must not claim it.
    //
    // A nanobind translator is registered ONCE for the whole process,
    // not per call, so this one is asked about every C++ exception any
    // module raises - including the mock's std::invalid_argument.
    // Rethrowing is how a translator says "not mine": nanobind then
    // tries the next one, and the last is its own, which maps
    // invalid_argument to ValueError and bad_alloc to MemoryError.
    //
    // Swallowing them here made every one of those a RuntimeError,
    // which is what Cython's per-method hook did and what nanobind's
    // process-wide one must not.
    //
    // An unmatched exception propagates out of `try { throw; }` on
    // its own, so there is no `throw;` to write - and writing one
    // after the chain would rethrow the exceptions this DID handle.
}

}  // namespace cythonix
