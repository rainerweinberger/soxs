import numpy as np
import subprocess
import tempfile
import shutil
import os
from soxs.utils import soxs_files_path, mylog, \
    parse_prng, parse_value, soxs_cfg, line_width_equiv, \
    DummyPbar
from soxs.lib.broaden_lines import broaden_lines
from soxs.constants import erg_per_keV, hc, \
    cosmic_elem, metal_elem, atomic_weights, clight, \
    m_u, elem_names, sigma_to_fwhm, abund_tables, sqrt2pi
import astropy.io.fits as pyfits
import astropy.units as u
import h5py
from scipy.interpolate import InterpolatedUnivariateSpline
from astropy.modeling.functional_models import \
    Gaussian1D
import glob
from tqdm import tqdm


class Energies(u.Quantity):
    def __new__(cls, energy, flux):
        ret = u.Quantity.__new__(cls, energy, unit="keV")
        ret.flux = u.Quantity(flux, "erg/(cm**2*s)")
        return ret


def _generate_energies(spec, t_exp, rate, prng, quiet=False):
    cumspec = spec.cumspec
    n_ph = prng.poisson(t_exp*rate)
    if not quiet:
        mylog.info("Creating %d energies from this spectrum." % n_ph)
    randvec = prng.uniform(size=n_ph)
    randvec.sort()
    e = np.interp(randvec, cumspec, spec.ebins.value)
    if not quiet:
        mylog.info("Finished creating energies.")
    return e


class Spectrum(object):
    _units = "photon/(cm**2*s*keV)"

    def __init__(self, ebins, flux):
        self.ebins = u.Quantity(ebins, "keV")
        self.emid = 0.5*(self.ebins[1:]+self.ebins[:-1])
        self.flux = u.Quantity(flux, self._units)
        self.nbins = len(self.emid)
        self.de = self.ebins[1]-self.ebins[0]
        self._compute_total_flux()

    def _compute_total_flux(self):
        self.total_flux = self.flux.sum()*self.de
        self.total_energy_flux = (self.flux*self.emid.to("erg")).sum()*self.de/(1.0*u.photon)
        cumspec = np.cumsum(self.flux.value*self.de.value)
        cumspec = np.insert(cumspec, 0, 0.0)
        cumspec /= cumspec[-1]
        self.cumspec = cumspec
        self.func = lambda e: np.interp(e, self.emid.value, self.flux.value)

    def __add__(self, other):
        if self.nbins != other.nbins or \
           not np.isclose(self.ebins.value, other.ebins.value).all():
            raise RuntimeError("Energy binning for these two "
                               "spectra is not the same!!")
        if self._units != other._units:
            raise RuntimeError("The units for these two spectra "
                               "are not the same!")
        return Spectrum(self.ebins, self.flux+other.flux)

    def __mul__(self, other):
        if hasattr(other, "eff_area"):
            return ConvolvedSpectrum(self, other)
        else:
            return Spectrum(self.ebins, other*self.flux)

    __rmul__ = __mul__

    def __truediv__(self, other):
        return Spectrum(self.ebins, self.flux/other)

    __div__ = __truediv__

    def __repr__(self):
        s = "Spectrum (%s - %s)\n" % (self.ebins[0], self.ebins[-1])
        s += "    Total Flux:\n    %s\n    %s\n" % (self.total_flux, self.total_energy_flux)
        return s

    def __call__(self, e):
        if hasattr(e, "to_astropy"):
            e = e.to_astropy()
        if isinstance(e, u.Quantity):
            e = e.to("keV").value
        return u.Quantity(self.func(e), self._units)

    def get_flux_in_band(self, emin, emax):
        """
        Determine the total flux within a band specified 
        by an energy range. 

        Parameters
        ----------
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy in the band, in keV.
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy in the band, in keV.

        Returns
        -------
        A tuple of values for the flux/intensity in the 
        band: the first value is in terms of the photon 
        rate, the second value is in terms of the energy rate. 
        """
        emin = parse_value(emin, "keV")
        emax = parse_value(emax, "keV")
        range = np.logical_and(self.emid.value >= emin, self.emid.value <= emax)
        pflux = self.flux[range].sum()*self.de
        eflux = (self.flux*self.emid.to("erg"))[range].sum()*self.de/(1.0*u.photon)
        return pflux, eflux

    @classmethod
    def from_xspec_script(cls, infile, emin, emax, nbins):
        """
        Create a model spectrum using a script file as 
        input to XSPEC.

        Parameters
        ----------
        infile : string
            Path to the script file to use. 
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy of the spectrum in keV. 
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy of the spectrum in keV. 
        nbins : integer
            The number of bins in the spectrum.
        """
        f = open(infile, "r")
        xspec_in = f.readlines()
        f.close()
        return cls._from_xspec(xspec_in, emin, emax, nbins)

    @classmethod
    def from_xspec_model(cls, model_string, params, emin, emax, nbins):
        """
        Create a model spectrum using a model string and parameters
        as input to XSPEC.

        Parameters
        ----------
        model_string : string
            The model to create the spectrum from. Use standard XSPEC
            model syntax. Example: "wabs*mekal"
        params : list
            The list of parameters for the model. Must be in the order
            that XSPEC expects.
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy of the spectrum in keV
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy of the spectrum in keV
        nbins : integer
            The number of bins in the spectrum.
        """
        xspec_in = []
        model_str = "%s &" % model_string
        for param in params:
            model_str += " %g &" % param
        model_str += " /*"
        xspec_in.append("model %s\n" % model_str)
        return cls._from_xspec(xspec_in, emin, emax, nbins)

    @classmethod
    def from_xspec(cls, model_string, params, emin, emax, nbins):
        mylog.warning("The 'from_xspec' method has been deprecated: "
                      "use 'from_xspec_model' instead.")
        cls.from_xspec_model(model_string, params, emin, emax, nbins)

    @classmethod
    def _from_xspec(cls, xspec_in, emin, emax, nbins):
        emin = parse_value(emin, "keV")
        emax = parse_value(emax, "keV")
        tmpdir = tempfile.mkdtemp()
        curdir = os.getcwd()
        os.chdir(tmpdir)
        xspec_in.append("dummyrsp %g %g %d lin\n" % (emin, emax, nbins))
        xspec_in += ["set fp [open spec_therm.xspec w+]\n",
                     "tclout energies\n", "puts $fp $xspec_tclout\n",
                     "tclout modval\n", "puts $fp $xspec_tclout\n",
                     "close $fp\n", "quit\n"]
        f_xin = open("xspec.in", "w")
        f_xin.writelines(xspec_in)
        f_xin.close()
        logfile = os.path.join(curdir, "xspec.log")
        with open(logfile, "ab") as xsout:
            subprocess.call(["xspec", "-", "xspec.in"],
                            stdout=xsout, stderr=xsout)
        f_s = open("spec_therm.xspec", "r")
        lines = f_s.readlines()
        f_s.close()
        ebins = np.array(lines[0].split()).astype("float64")
        de = np.diff(ebins)[0]
        flux = np.array(lines[1].split()).astype("float64")/de
        os.chdir(curdir)
        shutil.rmtree(tmpdir)
        return cls(ebins, flux)

    @classmethod
    def from_powerlaw(cls, photon_index, redshift, norm, emin, emax,
                      nbins):
        """
        Create a spectrum from a power-law model.

        Parameters
        ----------
        photon_index : float
            The photon index of the source.
        redshift : float
            The redshift of the source.
        norm : float
            The normalization of the source in units of
            photons/s/cm**2/keV at 1 keV in the source 
            frame.
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy of the spectrum in keV. 
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy of the spectrum in keV. 
        nbins : integer
            The number of bins in the spectrum. 
        """
        emin = parse_value(emin, 'keV')
        emax = parse_value(emax, 'keV')
        ebins = np.linspace(emin, emax, nbins+1)
        emid = 0.5*(ebins[1:]+ebins[:-1])
        flux = norm*(emid*(1.0+redshift))**(-photon_index)
        return cls(ebins, flux)

    @classmethod
    def from_file(cls, filename):
        """
        Read a spectrum from an ASCII or HDF5 file.

        If ASCII: accepts a file with two columns,
        the first being the center energy of the bin in 
        keV and the second being the spectrum in the
        appropriate units, assuming a linear binning 
        with constant bin widths.

        If HDF5: accepts a file with one array dataset, 
        named "spectrum", which is the spectrum in the 
        appropriate units, and two scalar datasets, 
        "emin" and "emax", which are the minimum and 
        maximum energies in keV.

        Parameters
        ----------
        filename : string
            The path to the file containing the spectrum.
        """
        if filename.endswith(".h5"):
            f = h5py.File(filename, "r")
            flux = f["spectrum"][()]
            nbins = flux.size
            ebins = np.linspace(f["emin"][()], f["emax"][()], nbins+1)
            f.close()
        else:
            emid, flux = np.loadtxt(filename, unpack=True)
            de = np.diff(emid)[0]
            ebins = np.append(emid-0.5*de, emid[-1]+0.5*de)
        return cls(ebins, flux)

    @classmethod
    def from_constant(cls, const_flux, emin, emax, nbins):
        """
        Create a spectrum from a constant model using 
        XSPEC.

        Parameters
        ----------
        const_flux : float
            The value of the constant flux in the units 
            of the spectrum. 
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy of the spectrum in keV. 
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy of the spectrum in keV. 
        nbins : integer
            The number of bins in the spectrum.
        """
        emin = parse_value(emin, "keV")
        emax = parse_value(emax, 'keV')
        ebins = np.linspace(emin, emax, nbins+1)
        flux = const_flux*np.ones(nbins)
        return cls(ebins, flux)

    def new_spec_from_band(self, emin, emax):
        """
        Create a new :class:`~soxs.spectra.Spectrum` object
        from a subset of an existing one defined by a particular
        energy band.

        Parameters
        ----------
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy of the band in keV.
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy of the band in keV.
        """
        emin = parse_value(emin, "keV")
        emax = parse_value(emax, 'keV')
        band = np.logical_and(self.ebins.value >= emin, 
                              self.ebins.value <= emax)
        idxs = np.where(band)[0]
        ebins = self.ebins.value[idxs]
        flux = self.flux.value[idxs[:-1]]
        return Spectrum(ebins, flux)

    def rescale_flux(self, new_flux, emin=None, emax=None, flux_type="photons"):
        """
        Rescale the flux of the spectrum, optionally using 
        a specific energy band.

        Parameters
        ----------
        new_flux : float
            The new flux in units of photons/s/cm**2.
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`, optional
            The minimum energy of the band to consider, 
            in keV. Default: Use the minimum energy of 
            the entire spectrum.
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`, optional
            The maximum energy of the band to consider, 
            in keV. Default: Use the maximum energy of 
            the entire spectrum.
        flux_type : string, optional
            The units of the flux to use in the rescaling:
                "photons": photons/s/cm**2
                "energy": erg/s/cm**2
        """
        if emin is None:
            emin = self.ebins[0].value
        if emax is None:
            emax = self.ebins[-1].value
        emin = parse_value(emin, "keV")
        emax = parse_value(emax, 'keV')
        idxs = np.logical_and(self.emid.value >= emin, self.emid.value <= emax)
        if flux_type == "photons":
            f = self.flux[idxs].sum()*self.de
        elif flux_type == "energy":
            f = (self.flux*self.emid.to("erg"))[idxs].sum()*self.de
        self.flux *= new_flux/f.value
        self._compute_total_flux()

    def write_file(self, specfile, overwrite=False):
        """
        Write the spectrum to a file.

        Parameters
        ----------
        specfile : string
            The filename to write the file to.
        overwrite : boolean, optional
            Whether or not to overwrite an existing 
            file with the same name. Default: False
        """
        if os.path.exists(specfile) and not overwrite:
            raise IOError("File %s exists and overwrite=False!" % specfile)
        header = "Energy\tFlux\nkeV\t%s" % self._units
        np.savetxt(specfile, np.transpose([self.emid, self.flux]), 
                   delimiter="\t", header=header)

    def write_h5_file(self, specfile, overwrite=False):
        """
        Write the spectrum to an HDF5 file.

        Parameters
        ----------
        specfile : string
            The filename to write the file to.
        overwrite : boolean, optional
            Whether or not to overwrite an existing 
            file with the same name. Default: False
        """
        if os.path.exists(specfile) and not overwrite:
            raise IOError("File %s exists and overwrite=False!" % specfile)
        f = h5py.File(specfile, "w")
        f.create_dataset("emin", data=self.ebins[0].value)
        f.create_dataset("emax", data=self.ebins[-1].value)
        f.create_dataset("spectrum", data=self.flux.value)
        f.close()

    def apply_foreground_absorption(self, nH, model="wabs", redshift=0.0):
        """
        Given a hydrogen column density, apply
        galactic foreground absorption to the spectrum.

        Parameters
        ----------
        nH : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The hydrogen column in units of 10**22 atoms/cm**2
        model : string, optional
            The model for absorption to use. Options are "wabs"
            (Wisconsin, Morrison and McCammon; ApJ 270, 119) or
            "tbabs" (Tuebingen-Boulder, Wilms, J., Allen, A., & 
            McCray, R. 2000, ApJ, 542, 914). Default: "wabs".
        redshift : float, optional
            The redshift of the absorbing material. Default: 0.0
        """
        nH = parse_value(nH, "1.0e22*cm**-2")
        e = self.emid.value*(1.0+redshift)
        if model == "wabs":
            sigma = wabs_cross_section(e)
        elif model == "tbabs":
            sigma = tbabs_cross_section(e)
        self.flux *= np.exp(-nH*1.0e22*sigma)
        self._compute_total_flux()

    def add_emission_line(self, line_center, line_width, line_amp,
                          line_type="gaussian"):
        """
        Add an emission line to this spectrum.

        Parameters
        ----------
        line_center : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The line center position in units of keV, in the observer frame.
        line_width : one or more float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The line width (FWHM) in units of keV, in the observer frame. Can also
            input the line width in units of velocity in the rest frame. For the Voigt
            profile, a list, tuple, or array of two values should be provided since there
            are two line widths, the Lorentzian and the Gaussian (in that order).
        line_amp : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The integrated line amplitude in the units of the flux 
        line_type : string, optional
            The line profile type. Default: "gaussian"
        """
        line_center = parse_value(line_center, "keV")
        line_width = parse_value(line_width, "keV", equivalence=line_width_equiv(line_center))
        line_amp = parse_value(line_amp, self._units)
        if line_type == "gaussian":
            sigma = line_width / sigma_to_fwhm
            line_amp /= sqrt2pi * sigma
            f = Gaussian1D(line_amp, line_center, sigma)
        else:
            raise NotImplementedError("Line profile type '%s' " % line_type +
                                      "not implemented!")
        self.flux += u.Quantity(f(self.emid.value), self._units)
        self._compute_total_flux()

    def add_absorption_line(self, line_center, line_width, equiv_width, 
                            line_type='gaussian'):
        """
        Add an absorption line to this spectrum.

        Parameters
        ----------
        line_center : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The line center position in units of keV, in the observer frame.
        line_width : one or more float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The line width (FWHM) in units of keV, in the observer frame. Can also
            input the line width in units of velocity in the rest frame. For the Voigt
            profile, a list, tuple, or array of two values should be provided since there
            are two line widths, the Lorentzian and the Gaussian (in that order).
        equiv_width : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The equivalent width of the line, in units of milli-Angstrom
        line_type : string, optional
            The line profile type. Default: "gaussian"
        """
        line_center = parse_value(line_center, "keV")
        line_width = parse_value(line_width, "keV", equivalence=line_width_equiv(line_center))
        equiv_width = parse_value(equiv_width, "1.0e-3*angstrom") # in milliangstroms
        equiv_width *= 1.0e-3 # convert to angstroms
        if line_type == "gaussian":
            sigma = line_width / sigma_to_fwhm
            B = equiv_width*line_center*line_center
            B /= hc * sqrt2pi * sigma
            f = Gaussian1D(B, line_center, sigma)
        else:
            raise NotImplementedError("Line profile type '%s' " % line_type +
                                      "not implemented!")
        self.flux *= np.exp(-f(self.emid.value))
        self._compute_total_flux()

    def generate_energies(self, t_exp, area, prng=None, quiet=False):
        """
        Generate photon energies from this spectrum 
        given an exposure time and effective area.

        Parameters
        ----------
        t_exp : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The exposure time in seconds.
        area : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The effective area in cm**2. If one is creating 
            events for a SIMPUT file, a constant should be 
            used and it must be large enough so that a 
            sufficiently large sample is drawn for the ARF.
        prng : :class:`~numpy.random.RandomState` object, integer, or None
            A pseudo-random number generator. Typically will only 
            be specified if you have a reason to generate the same 
            set of random numbers, such as for a test. Default is None, 
            which sets the seed based on the system time. 
        quiet : boolean, optional
            If True, log messages will not be displayed when 
            creating energies. Useful if you have to loop over 
            a lot of spectra. Default: False
        """
        t_exp = parse_value(t_exp, "s")
        area = parse_value(area, "cm**2")
        prng = parse_prng(prng)
        rate = area*self.total_flux.value
        energy = _generate_energies(self, t_exp, rate, prng, quiet=quiet)
        flux = np.sum(energy)*erg_per_keV/t_exp/area
        energies = Energies(energy, flux)
        return energies

    def plot(self, lw=2, xmin=None, xmax=None, ymin=None, ymax=None,
             xscale=None, yscale=None, label=None, fontsize=18, 
             fig=None, ax=None, **kwargs):
        """
        Make a quick Matplotlib plot of the spectrum. A Matplotlib
        figure and axis is returned.

        Parameters
        ----------
        lw : float, optional
            The width of the lines in the plots. Default: 2.0 px.
        xmin : float, optional
            The left-most energy in keV to plot. Default is the 
            minimum value in the spectrum. 
        xmax : float, optional
            The right-most energy in keV to plot. Default is the 
            maximum value in the spectrum. 
        ymin : float, optional
            The lower extent of the y-axis. By default it is set automatically.
        ymax : float, optional
            The upper extent of the y-axis. By default it is set automatically.
        xscale : string, optional
            The scaling of the x-axis of the plot. Default: "log"
        yscale : string, optional
            The scaling of the y-axis of the plot. Default: "log"
        label : string, optional
            The label of the spectrum. Default: None
        fontsize : int
            Font size for labels and axes. Default: 18
        fig : :class:`~matplotlib.figure.Figure`, optional
            A Figure instance to plot in. Default: None, one will be
            created if not provided.
        ax : :class:`~matplotlib.axes.Axes`, optional
            An Axes instance to plot in. Default: None, one will be
            created if not provided.

        Returns
        -------
        A tuple of the :class:`~matplotlib.figure.Figure` and the :class:`~matplotlib.axes.Axes` objects.
        """
        import matplotlib.pyplot as plt
        if fig is None:
            fig = plt.figure(figsize=(10, 10))
        if xscale is None:
            if ax is None:
                xscale = "log"
            else:
                xscale = ax.get_xscale()
        if yscale is None:
            if ax is None:
                yscale = "log"
            else:
                yscale = ax.get_yscale()
        if ax is None:
            ax = fig.add_subplot(111)
        ax.plot(self.emid, self.flux, lw=lw, label=label, **kwargs)
        ax.set_xscale(xscale)
        ax.set_yscale(yscale)
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        ax.set_xlabel("Energy (keV)", fontsize=fontsize)
        yunit = u.Unit(self._units).to_string("latex").replace("{}^{\\prime}", "arcmin")
        ax.set_ylabel("Spectrum (%s)" % yunit, fontsize=fontsize)
        ax.tick_params(axis='both',labelsize=fontsize)
        return fig, ax


class ApecGenerator(object):
    r"""
    Initialize a thermal gas emission model from the 
    AtomDB APEC tables available at http://www.atomdb.org. 
    This code borrows heavily from Python routines used to 
    read the APEC tables developed by Adam Foster at the
    CfA (afoster@cfa.harvard.edu).

    Parameters
    ----------
    emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
        The minimum energy for the spectral model.
    emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
        The maximum energy for the spectral model.
    nbins : integer
        The number of bins in the spectral model.
    var_elem : list of strings, optional
        The names of elements to allow to vary freely
        from the single abundance parameter. These can be strings like
        ["O", "N", "He"], or if nei=True they must be elements with
        ionization states, e.g. ["O^1", "O^2", "N^4"]. Default:
        None
    apec_root : string, optional
        The directory root where the APEC model files 
        are stored. If not provided, the default is to 
        grab them from the tables stored with SOXS.
    apec_vers : string, optional
        The version identifier string for the APEC files. 
        Default: "3.0.9"
    broadening : boolean, optional
        Whether or not the spectral lines should be 
        thermally and velocity broadened. Default: True
    nolines : boolean, optional
        Turn off lines entirely for generating spectra.
        Default: False
    abund_table : string or array_like, optional
        The abundance table to be used for solar abundances. 
        Either a string corresponding to a built-in table or an array
        of 30 floats corresponding to the abundances of each element
        relative to the abundance of H. Default is set in the SOXS
        configuration file, the default for which is "angr".
        Built-in options are:
        "angr" : from Anders E. & Grevesse N. (1989, Geochimica et 
        Cosmochimica Acta 53, 197)
        "aspl" : from Asplund M., Grevesse N., Sauval A.J. & Scott 
        P. (2009, ARAA, 47, 481)
        "wilm" : from Wilms, Allen & McCray (2000, ApJ 542, 914 
        except for elements not listed which are given zero abundance)
        "lodd" : from Lodders, K (2003, ApJ 591, 1220)
    nei : boolean, optional
        If True, use the non-equilibrium ionization tables. These are
        not supplied with SOXS but must be downloaded separately, in
        which case the *apec_root* parameter must also be set to their
        location. Default: False

    Examples
    --------
    >>> apec_model = ApecGenerator(0.05, 50.0, 1000, apec_vers="3.0.3",
    ...                            broadening=True)
    """
    def __init__(self, emin, emax, nbins, var_elem=None, apec_root=None,
                 apec_vers=None, broadening=True, nolines=False,
                 abund_table=None, nei=False):
        if apec_vers is None:
            filedir = os.path.join(os.path.dirname(__file__), 'files')
            cfile = glob.glob("%s/apec_*_coco.fits" % filedir)[0]
            apec_vers = cfile.split("/")[-1].split("_")[1][1:]
        mylog.info("Using APEC version %s." % apec_vers)
        if nei and apec_root is None:
            raise RuntimeError("The NEI APEC tables are not supplied with "
                               "SOXS! Download them from http://www.atomdb.org "
                               "and set 'apec_root' to their location.")
        if nei and var_elem is None:
            raise RuntimeError("For NEI spectra, you must specify which elements "
                               "you want to vary using the 'var_elem' argument!")
        self.nei = nei
        emin = parse_value(emin, "keV")
        emax = parse_value(emax, 'keV')
        self.emin = emin
        self.emax = emax
        self.nbins = nbins
        self.ebins = np.linspace(self.emin, self.emax, nbins+1)
        self.de = np.diff(self.ebins)
        self.emid = 0.5*(self.ebins[1:]+self.ebins[:-1])
        if apec_root is None:
            apec_root = soxs_files_path
        if nei:
            neistr = "_nei"
            ftype = "comp"
        else:
            neistr = ""
            ftype = "coco"
        self.cocofile = os.path.join(apec_root, "apec_v%s%s_%s.fits" % (apec_vers, neistr, ftype))
        self.linefile = os.path.join(apec_root, "apec_v%s%s_line.fits" % (apec_vers, neistr))
        if not os.path.exists(self.cocofile) or not os.path.exists(self.linefile):
            raise IOError("Cannot find the APEC files!\n %s\n, %s" % (self.cocofile,
                                                                      self.linefile))
        mylog.info("Using %s for generating spectral lines." % os.path.split(self.linefile)[-1])
        mylog.info("Using %s for generating the continuum." % os.path.split(self.cocofile)[-1])
        self.nolines = nolines
        self.wvbins = hc/self.ebins[::-1]
        self.broadening = broadening
        try:
            self.line_handle = pyfits.open(self.linefile)
        except IOError:
            raise IOError("Line file %s does not exist" % self.linefile)
        try:
            self.coco_handle = pyfits.open(self.cocofile)
        except IOError:
            raise IOError("Continuum file %s does not exist" % self.cocofile)
        self.Tvals = self.line_handle[1].data.field("kT")
        self.nT = len(self.Tvals)
        self.dTvals = np.diff(self.Tvals)
        self.minlam = self.wvbins.min()
        self.maxlam = self.wvbins.max()
        self.var_elem_names = []
        self.var_ion_names = []
        if var_elem is None:
            self.var_elem = np.empty((0,1), dtype='int')
        else:
            self.var_elem = []
            if len(var_elem) != len(set(var_elem)):
                raise RuntimeError("Duplicates were found in the \"var_elem\" list! %s" % var_elem)
            for elem in var_elem:
                if "^" in elem:
                    if not self.nei:
                        raise RuntimeError("Cannot use different ionization states with a "
                                           "CIE plasma!")
                    el = elem.split("^")
                    e = el[0]
                    ion = int(el[1])
                else:
                    if self.nei:
                        raise RuntimeError("Variable elements must include the ionization "
                                           "state for NEI plasmas!")
                    e = elem
                    ion = 0
                self.var_elem.append([elem_names.index(e), ion])
            self.var_elem.sort(key=lambda x: (x[0], x[1]))
            self.var_elem = np.array(self.var_elem, dtype='int')
            self.var_elem_names = [elem_names[e[0]] for e in self.var_elem]
            self.var_ion_names = ["%s^%d" % (elem_names[e[0]], e[1]) for e in self.var_elem]
        self.num_var_elem = len(self.var_elem)
        if self.nei:
            self.cosmic_elem = [elem for elem in [1, 2]
                                if elem not in self.var_elem[:, 0]]
            self.metal_elem = []
        else:
            self.cosmic_elem = [elem for elem in cosmic_elem 
                                if elem not in self.var_elem[:,0]]
            self.metal_elem = [elem for elem in metal_elem
                               if elem not in self.var_elem[:,0]]
        if abund_table is None:
            abund_table = soxs_cfg.get("soxs", "abund_table")
        if not isinstance(abund_table, str):
            if len(abund_table) != 30:
                raise RuntimeError("User-supplied abundance tables "
                                   "must be 30 elements long!")
            self.atable = np.concatenate([[0.0], np.array(abund_table)])
        else:
            self.atable = abund_tables[abund_table].copy()
        self._atable = self.atable.copy()
        self._atable[1:] /= abund_tables["angr"][1:]

    def _make_spectrum(self, kT, element, ion, velocity, line_fields,
                       coco_fields, scale_factor):

        tmpspec = np.zeros(self.nbins)

        if not self.nolines:
            loc = (line_fields['element'] == element) & \
                  (line_fields['lambda'] > self.minlam) & \
                  (line_fields['lambda'] < self.maxlam)
            if self.nei:
                loc &= (line_fields['ion_drv'] == ion+1)
            i = np.where(loc)[0]
            E0 = hc/line_fields['lambda'][i].astype("float64")*scale_factor
            amp = line_fields['epsilon'][i].astype("float64")*self._atable[element]
            if self.broadening:
                sigma = 2.*kT*erg_per_keV/(atomic_weights[element]*m_u)
                sigma += 2.0*velocity*velocity
                sigma = E0*np.sqrt(sigma)/clight
                vec = broaden_lines(E0, sigma, amp, self.ebins)
            else:
                vec = np.histogram(E0, self.ebins, weights=amp)[0]
            tmpspec += vec

        ind = np.where((coco_fields['Z'] == element) &
                       (coco_fields['rmJ'] == ion+int(self.nei)))[0]

        if len(ind) == 0:
            return tmpspec
        else:
            ind = ind[0]

        de0 = self.de/scale_factor

        n_cont = coco_fields['N_Cont'][ind]
        e_cont = coco_fields['E_Cont'][ind][:n_cont]*scale_factor
        continuum = coco_fields['Continuum'][ind][:n_cont]*self._atable[element]

        tmpspec += np.interp(self.emid, e_cont, continuum)*de0

        n_pseudo = coco_fields['N_Pseudo'][ind]
        e_pseudo = coco_fields['E_Pseudo'][ind][:n_pseudo]*scale_factor
        pseudo = coco_fields['Pseudo'][ind][:n_pseudo]*self._atable[element]

        tmpspec += np.interp(self.emid, e_pseudo, pseudo)*de0

        return tmpspec*scale_factor

    def _preload_data(self, index):
        line_data = self.line_handle[index+2].data
        coco_data = self.coco_handle[index+2].data
        line_fields = ['element', 'lambda', 'epsilon']
        if self.nei:
            line_fields.append('ion_drv')
        line_fields = tuple(line_fields)
        coco_fields = ('Z', 'rmJ', 'N_Cont', 'E_Cont', 'Continuum',
                       'N_Pseudo','E_Pseudo', 'Pseudo')
        line_fields = {el: line_data.field(el) for el in line_fields}
        coco_fields = {el: coco_data.field(el) for el in coco_fields}
        return line_fields, coco_fields

    def _get_table(self, indices, redshift, velocity):
        numi = len(indices)
        scale_factor = 1./(1.+redshift)
        cspec = np.zeros((numi, self.nbins))
        mspec = np.zeros((numi, self.nbins))
        vspec = None
        if self.num_var_elem > 0:
            vspec = np.zeros((self.num_var_elem, numi, self.nbins))
        if numi > 2:
            pbar = tqdm(leave=True, total=numi, desc="Preparing spectrum table ")
        else:
            pbar = DummyPbar()
        for i, ikT in enumerate(indices):
            line_fields, coco_fields = self._preload_data(ikT)
            # First do H, He, and trace elements
            for elem in self.cosmic_elem:
                if self.nei:
                    # For H, He we assume fully ionized
                    ion = elem
                else:
                    ion = 0
                cspec[i,:] += self._make_spectrum(self.Tvals[ikT], elem, ion, velocity, line_fields,
                                                  coco_fields, scale_factor)
            # Next do the metals
            for elem in self.metal_elem:
                mspec[i,:] += self._make_spectrum(self.Tvals[ikT], elem, 0, velocity, line_fields,
                                                  coco_fields, scale_factor)
            # Now do any metals that we wanted to vary freely from the abund
            # parameter
            if self.num_var_elem > 0:
                for j, elem in enumerate(self.var_elem):
                    vspec[j,i,:] = self._make_spectrum(self.Tvals[ikT], elem[0], elem[1],
                                                       velocity, line_fields, coco_fields, scale_factor)
            pbar.update()
        pbar.close()
        return cspec, mspec, vspec

    def _spectrum_init(self, kT, velocity, elem_abund):
        kT = parse_value(kT, "keV")
        velocity = parse_value(velocity, "km/s")
        v = velocity*1.0e5
        tindex = np.searchsorted(self.Tvals, kT)-1
        dT = (kT-self.Tvals[tindex])/self.dTvals[tindex]
        return kT, dT, tindex, v

    def get_spectrum(self, kT, abund, redshift, norm, velocity=0.0,
                     elem_abund=None):
        """
        Get a thermal emission spectrum assuming CIE.

        Parameters
        ----------
        kT : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The temperature in keV.
        abund : float
            The metal abundance in solar units. 
        redshift : float
            The redshift.
        norm : float
            The normalization of the model, in the standard
            Xspec units of 1.0e-14*EM/(4*pi*(1+z)**2*D_A**2).
        velocity : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`, optional
            The velocity broadening parameter, in units of 
            km/s. Default: 0.0
        elem_abund : dict of element name, float pairs, optional
            A dictionary of elemental abundances in solar
            units to vary freely of the abund parameter, e.g.
            {"O": 0.4, "N": 0.3, "He": 0.9}. Default: None
        """
        if self.nei:
            raise RuntimeError("Use 'get_nei_spectrum' for NEI spectra!")
        if elem_abund is None:
            elem_abund = {}
        if set(elem_abund.keys()) != set(self.var_elem_names):
            raise RuntimeError("The supplied set of abundances is not the "
                               "same as that was originally set!\n"
                               "Free elements: %s\nAbundances: %s" % (set(elem_abund.keys()),
                                                                      set(self.var_elem_names)))
        kT, dT, tindex, v = self._spectrum_init(kT, velocity, elem_abund)
        if tindex >= self.Tvals.shape[0]-1 or tindex < 0:
            return np.zeros(self.nbins)
        cspec, mspec, vspec = self._get_table([tindex, tindex+1], redshift, v)
        cosmic_spec = cspec[0,:]*(1.-dT)+cspec[1,:]*dT
        metal_spec = mspec[0,:]*(1.-dT)+mspec[1,:]*dT
        spec = cosmic_spec + abund*metal_spec
        if vspec is not None:
            for elem, eabund in elem_abund.items():
                j = self.var_elem_names.index(elem)
                spec += eabund*(vspec[j,0,:]*(1.-dT)+vspec[j,1,:]*dT)
        spec = 1.0e14*norm*spec/self.de
        return Spectrum(self.ebins, spec)

    def get_nei_spectrum(self, kT, elem_abund, redshift, norm, velocity=0.0):
        """
        Get a thermal emission spectrum assuming NEI.

        Parameters
        ----------
        kT : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The temperature in keV.
        elem_abund : dict of element name, float pairs
            A dictionary of ionization state abundances in solar
            units to vary freely of the abund parameter, e.g.
            {"O^1": 0.4, "O^4": 0.6, "N^2": 0.7} Default: None
        redshift : float
            The redshift.
        norm : float
            The normalization of the model, in the standard
            Xspec units of 1.0e-14*EM/(4*pi*(1+z)**2*D_A**2).
        velocity : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`, optional
            The velocity broadening parameter, in units of 
            km/s. Default: 0.0
        """
        if not self.nei:
            raise RuntimeError("Use 'get_spectrum' for CIE spectra!")
        if set(elem_abund.keys()) != set(self.var_ion_names):
            raise RuntimeError("The supplied set of abundances is not the "
                               "same as that was originally set!\n"
                               "Free elements: %s\nAbundances: %s" % (set(elem_abund.keys()),
                                                                      set(self.var_ion_names)))
        kT, dT, tindex, v = self._spectrum_init(kT, velocity, elem_abund)
        if tindex >= self.Tvals.shape[0]-1 or tindex < 0:
            return np.zeros(self.nbins)
        cspec, _, vspec = self._get_table([tindex, tindex+1], redshift, v)
        spec = cspec[0,:]*(1.-dT)+cspec[1,:]*dT
        for elem, eabund in elem_abund.items():
            j = self.var_ion_names.index(elem)
            spec += eabund*(vspec[j,0,:]*(1.-dT) + vspec[j,1,:]*dT)
        spec = 1.0e14*norm*spec/self.de
        return Spectrum(self.ebins, spec)


def wabs_cross_section(E):
    emax = np.array([0.0, 0.1, 0.284, 0.4, 0.532, 0.707, 0.867, 1.303, 1.840, 
                     2.471, 3.210, 4.038, 7.111, 8.331, 10.0])
    c0 = np.array([17.3, 34.6, 78.1, 71.4, 95.5, 308.9, 120.6, 141.3,
                   202.7,342.7,352.2,433.9,629.0,701.2])
    c1 = np.array([608.1, 267.9, 18.8, 66.8, 145.8, -380.6, 169.3,
                   146.8, 104.7, 18.7, 18.7, -2.4, 30.9, 25.2]) 
    c2 = np.array([-2150., -476.1 ,4.3, -51.4, -61.1, 294.0, -47.7,
                   -31.5, -17.0, 0.0, 0.0, 0.75, 0.0, 0.0])
    idxs = np.minimum(np.searchsorted(emax, E)-1, 13)
    sigma = (c0[idxs]+c1[idxs]*E+c2[idxs]*E*E)*1.0e-24/E**3
    return sigma


def get_wabs_absorb(e, nH):
    sigma = wabs_cross_section(e)
    return np.exp(-nH*1.0e22*sigma)


_tbabs_emid = None
_tbabs_sigma = None
_tbabs_spline = None


def tbabs_cross_section(E):
    global _tbabs_emid
    global _tbabs_sigma
    global _tbabs_spline
    if _tbabs_spline is None:
        filename = os.path.join(soxs_files_path, "tbabs_table.h5")
        f = h5py.File(filename, "r")
        _tbabs_sigma = f["cross_section"][:]
        nbins = _tbabs_sigma.size
        ebins = np.linspace(f["emin"][()], f["emax"][()], nbins+1)
        f.close()
        _tbabs_emid = 0.5*(ebins[1:]+ebins[:-1])
        _tbabs_spline = InterpolatedUnivariateSpline(_tbabs_emid,
                                                     _tbabs_sigma, k=5, 
                                                     ext=1)
    return _tbabs_spline(E)


def get_tbabs_absorb(e, nH):
    sigma = tbabs_cross_section(e)
    return np.exp(-nH*1.0e22*sigma)


class ConvolvedSpectrum(Spectrum):
    _units = "photon/(s*keV)"

    def __init__(self, spectrum, arf):
        """
        Generate a convolved spectrum by convolving a spectrum with an
        ARF.

        Parameters
        ----------
        spectrum : :class:`~soxs.spectra.Spectrum` object
            The input spectrum to convolve with.
        arf : string or :class:`~soxs.instrument.AuxiliaryResponseFile`
            The ARF to use in the convolution.
        """
        from soxs.instrument import AuxiliaryResponseFile
        if not isinstance(arf, AuxiliaryResponseFile):
            arf = AuxiliaryResponseFile(arf)
        self.arf = arf
        earea = arf.interpolate_area(spectrum.emid.value)
        rate = spectrum.flux * earea
        super(ConvolvedSpectrum, self).__init__(spectrum.ebins, rate)

    def deconvolve(self):
        """
        Return the deconvolved :class:`~soxs.spectra.Spectrum`
        object associated with this convolved spectrum.
        """
        earea = self.arf.interpolate_area(self.emid)
        flux = self.flux / earea
        flux = np.nan_to_num(flux.value)
        return Spectrum(self.ebins.value, flux)

    def generate_energies(self, t_exp, prng=None, quiet=False):
        """
        Generate photon energies from this convolved spectrum given an
        exposure time.

        Parameters
        ----------
        t_exp : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The exposure time in seconds.
        prng : :class:`~numpy.random.RandomState` object, integer, or None
            A pseudo-random number generator. Typically will only 
            be specified if you have a reason to generate the same 
            set of random numbers, such as for a test. Default is None, 
            which sets the seed based on the system time.
        quiet : boolean, optional
            If True, log messages will not be displayed when 
            creating energies. Useful if you have to loop over 
            a lot of spectra. Default: False
        """
        t_exp = parse_value(t_exp, "s")
        prng = parse_prng(prng)
        rate = self.total_flux.value
        energy = _generate_energies(self, t_exp, rate, prng, quiet=quiet)
        earea = self.arf.interpolate_area(energy).value
        flux = np.sum(energy)*erg_per_keV/t_exp/earea.sum()
        energies = Energies(energy, flux)
        return energies

    def apply_foreground_absorption(self, nH, model="wabs"):
        raise NotImplementedError

    def rescale_flux(self, new_flux, emin=None, emax=None, flux_type="photons"):
        """
        Rescale the flux of the convolved spectrum, optionally using 
        a specific energy band.

        Parameters
        ----------
        new_flux : float
            The new flux in units of photons/s/cm**2.
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`, optional
            The minimum energy of the band to consider, 
            in keV. Default: Use the minimum energy of 
            the entire spectrum.
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`, optional
            The maximum energy of the band to consider, 
            in keV. Default: Use the maximum energy of 
            the entire spectrum.
        flux_type : string, optional
            The units of the flux to use in the rescaling:
                "photons": photons/s
                "energy": erg/s
        """
        super(ConvolvedSpectrum, self).rescale_flux(new_flux, emin=emin, emax=emax, 
                                                    flux_type=flux_type)

    @classmethod
    def from_constant(cls, const_flux, emin=0.01, emax=50.0, nbins=10000):
        raise NotImplementedError

    @classmethod
    def from_powerlaw(cls, photon_index, redshift, norm,
                      emin=0.01, emax=50.0, nbins=10000):
        raise NotImplementedError

    @classmethod
    def from_xspec_model(cls, model_string, params, emin=0.01, emax=50.0,
                         nbins=10000):
        raise NotImplementedError

    @classmethod
    def from_xspec_script(cls, infile, emin=0.01, emax=50.0, nbins=10000):
        raise NotImplementedError
