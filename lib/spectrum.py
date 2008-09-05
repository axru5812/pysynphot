"""
Module: spectrum.py

Contains SourceSpectrum and SpectralElement class defnitions and
their subclasses.

Also contains the Vega object, which is an instance of a FileSourceSpectrum
that can be imported from this file and used for Vega-related calculations.

Dependencies:
=============
pyfits, numpy


"""

import string
import re
import os
import math

import pyfits
import numpy as N
from numpy import ma as MA

import units
import observationmode
import locations
import planck




# Renormalization constants from synphot:
PI = 3.14159265               # Mysterious math constant
RSUN = 6.9599E10              # Radius of sun
PC = 3.085678E18              # Parsec
RADIAN = RSUN / PC /1000.
RENORM = PI * RADIAN * RADIAN # Normalize to 1 solar radius @ 1 kpc

def _computeDefaultWaveset():
    minwave = 500.
    maxwave = 26000.
    lenwave = 10000

    w1 = math.log10(minwave)
    w2 = math.log10(maxwave)

    result = N.zeros(shape=[lenwave,],dtype=N.float64)

    for i in range(lenwave):
        frac = float(i) / lenwave
        result[i] = 10 ** (w1 * (1.0 - frac) + w2 * frac)

    return result        

# Default waveset is computed at load time, once and for all.
# Note that this is not thread safe.
global default_waveset
default_waveset = _computeDefaultWaveset()


def MergeWaveSets(waveset1, waveset2):
    """Return the union of the two wavesets, unless one or
    both of them is None."""
    if waveset1 is None and waveset2 is not None:
        MergedWaveSet = waveset2
    elif waveset2 is None and waveset1 is not None:
        MergedWaveSet = waveset1
    elif waveset1 is None and Waveset2 is None:
        MergedWaveSet = None
    else:
        MergedWaveSet = N.sort(N.union1d(waveset1, waveset2))

    return MergedWaveSet


def trimSpectrum(sp, minw, maxw):
    ''' Creates a new spectrum with trimmed upper and lower ranges.
    '''
    wave = sp.GetWaveSet()
    flux = sp(wave)

    new_wave = N.compress(wave >= minw, wave)
    new_flux = N.compress(wave >= minw, flux)

    new_wave = N.compress(new_wave <= maxw, new_wave)
    new_flux = N.compress(new_wave <= maxw, new_flux)

    result = TabularSourceSpectrum()

    result._wavetable = new_wave
    result._fluxtable = new_flux

    result.waveunits = units.Units(sp.waveunits.name)
    result.fluxunits = units.Units(sp.fluxunits.name)

    return result


class Integrator(object):
    ''' Integrator engine.
    '''
    def trapezoidIntegration(self,x,y):
        npoints = x.size
        indices = N.arange(npoints)[:-1]
        deltas = x[indices+1] - x[indices]
        integrand = 0.5*(y[indices+1] + y[indices])*deltas
        sum = integrand.sum()
        if x[-1]<x[0]:
            sum*= -1.0
        return sum

    def _columnsFromASCII(self, filename):
        """ Following synphot/TABLES, ASCII files may contain blank lines,
        comment lines (beginning with '#'), or terminal comments. This routine
        may be called by both Spectrum and SpectralElement objects to extract
        the first two columns from a file."""

        wlist=[]
        flist=[]
        lcount=0
        fs = open(filename,mode='r')
        lines=fs.readlines()
        fs.close()
        for line in lines:
            lcount+=1
            cline=line.strip()
            if ((len(cline) > 0) and (not cline.startswith('#'))):
                try:
                    cols=cline.split()
                    if len(cols) >= 2: 
                        wlist.append(float(cols[0]))
                        flist.append(float(cols[1]))
                except Exception, e:
                    raise ValueError("Error reading %s: %s"%(filename,str(e)))

        return wlist, flist
                
    def validate_wavetable(self):
        "Enforce monotonic, ascending wavelengths with no zero values"
        #First check for invalid values
        wave=self._wavetable
        if N.any(wave <= 0):
            raise ValueError('Zero wavelength occurs in wavelength array: invalid value')


        #Now check for monotonicity & enforce ascending
        sorted=N.sort(wave)        
        if not N.alltrue(sorted == wave):
            if N.alltrue(sorted[::-1] == wave):
                #monotonic descending is allowed
                pass
            else:
                raise ValueError('Wavelength array is not monotonic: invalid')

    def validate_fluxtable(self):
        "Enforce non-negative fluxes"
        if ((not self.fluxunits.isMag) #neg. magnitudes are legal
            and (self._fluxtable.min() < 0)):
            idx=N.where(self._fluxtable < 0)
            self._fluxtable[idx]=0.0
            print "Warning, %d of %d bins contained negative fluxes; they have been set to zero."%(len(idx[0]),len(self._fluxtable))


class SourceSpectrum(Integrator):
    '''Base class for the Source Spectrum object.
    '''
    
    def __add__(self, other):
        '''Source Spectra can be added.  Delegate the work to the
        CompositeSourceSpectrum class.
        '''
        if not isinstance(other, SourceSpectrum):
            raise TypeError("Can only add two SourceSpectrum objects")

        return CompositeSourceSpectrum(self, other, 'add')

    def __mul__(self, other):
        '''Source Spectra can be multiplied, by constants or by
        SpectralElement objects.
        '''
        #Ack!! There has to be a better way to do this
        if type(other) in [type(1), type(1.0),N.float64]:
            other = UniformTransmission(other)
        if not isinstance(other, SpectralElement):
            raise TypeError("SourceSpectrum objects can only be multiplied by SpectralElement objects or constants; %s type detected"%type(other))


        ## Delegate the work of multiplying to CompositeSourceSpectrum

        return CompositeSourceSpectrum(self, other, 'multiply')

    def __rmul__(self, other):
        return self.__mul__(other)

    def addmag(self,magval):
        """Adding a magnitude is like multiplying a flux. Only works for
        numbers -- not arrays, spectrum objects, etc"""
        if N.isscalar(magval):
            factor = 10**(-0.4*magval)
            return self*factor
        else:
            raise TypeError(".addmag() only takes a constant scalar argument")
    
    def getArrays(self):
        '''Returns wavelength and flux arrays as a tuple, performing
           units conversion.
        '''
        wave = self.GetWaveSet();
        flux = self(wave)

        flux = units.Photlam().Convert(wave, flux, self.fluxunits.name)
        wave = units.Angstrom().Convert(wave, self.waveunits.name)

        return (wave, flux)

    #Define properties for consistent UI
    def _getWaveProp(self):
        wave,flux=self.getArrays()
        return wave

    def _getFluxProp(self):
        wave,flux=self.getArrays()
        return flux

    wave=property(_getWaveProp,doc="Wavelength property")
    flux=property(_getFluxProp,doc="Flux property")

    def validate_units(self):
        "Ensure that waveunits are WaveUnits and fluxunits are FluxUnits"
        if (not isinstance(self.waveunits,units.WaveUnits)):
            raise TypeError("%s is not a valid WaveUnit"%self.waveunits)
        if (not isinstance(self.fluxunits,units.FluxUnits)):
            raise TypeError("%s is not a valid FluxUnit"%self.fluxunits)

    def writefits(self, filename, clobber=True, trimzero=True,
                  binned=False):
        """Write the spectrum to a FITS file."""

        if clobber:
            try:
                os.remove(filename)
            except OSError:
                pass

        if binned:
            wave=self.binwave
            flux=self.binflux
        else:
            wave=self.wave
            flux=self.flux
            
        first,last=0,len(flux)
        if trimzero:
            #Keep one zero at each end
            nz = flux.nonzero()[0]
            try:
                first=max(nz[0]-1,first)
                last=min(nz[-1]+2,last)
            except IndexError:
                pass

        #Construct the columns and HDUlist
        cw = pyfits.Column(name='WAVELENGTH',
                           array=wave[first:last],
                           unit=self.waveunits.name,
                           format='E')
        cf = pyfits.Column(name='FLUX',
                           array=flux[first:last],
                           unit=self.fluxunits.name,
                           format='E')
        hdu = pyfits.PrimaryHDU()
        hdulist = pyfits.HDUList([hdu])

        #Write the file
        cols = pyfits.ColDefs([cw, cf])
        hdu = pyfits.new_table(cols)
        hdulist.append(hdu)
        hdu.writeto(filename)

           

                                                 
    def integrate(self,fluxunits='photlam'):
        u=self.fluxunits
        self.convert(fluxunits)

        wave,flux=self.getArrays()
        self.convert(u)
            
        return self.trapezoidIntegration(wave,flux)
    

    def sample(self,wave):
        """Return a flux array, in self.fluxunits, on the provided
        wavetable"""
        #First use the __call__ to get it in photlam
        flux=self(wave)
        #Then convert to the desired units

        ans=units.Photlam().Convert(wave,flux,self.fluxunits.name)
        return ans
    
    def convert(self, targetunits):
        '''Convert to other units. This method actually just changes the
        wavelength and flux units objects, it does not recompute the
        internally kept wave and flux data; these are kept always in internal
        units. Method getArrays does the actual computation.
        '''
        nunits = units.Units(targetunits)
        if nunits.isFlux:
            self.fluxunits = nunits
        else:
            self.waveunits = nunits

    def redshift(self, z):
        ''' Returns a new redshifted spectrum.
        '''
        # begin by building a copy of self, but in Tabular format.
        copy = TabularSourceSpectrum()
        copy.fluxunits = self.fluxunits
        copy.waveunits = self.waveunits
        copy._wavetable = self.GetWaveSet()
        copy._fluxtable = self(copy._wavetable)

        # flux expressed in photnu is invariant wrt redshift; convert
        # the copy to photnu and extract its flux array.
        copy.convert('photnu')
        (dummy, flux_photnu) = copy.getArrays()

        # convert wavelenght array in the copy, to the redshifted frame.
        copy._wavetable *= 1.0 + z

        # now comes the trick: put back the flux array in photnu units
        # into the copy, and convert it back to internal units.
        copy._fluxtable = flux_photnu
        copy.fluxunits = units.Units('photnu')
        copy.ToInternal()
        copy.fluxunits = self.fluxunits
        copy.name="%s at z=%f"%(self.name,z)

        return copy

    def setMagnitude(self, band, value):
        '''Makes the magnitude of the source in the band equal to value.
        band is a SpectralElement.
        This method is marked for deletion once the .renorm method is
        well tested.
        '''
        objectFlux = band.calcTotalFlux(self)
        vegaFlux = band.calcVegaFlux()
        magDiff = -2.5*math.log10(objectFlux/vegaFlux)
        factor = 10**(-0.4*(value - magDiff))

        '''Object returned is a CompositeSourceSpectrum'''

        return self * factor

    def renorm(self, RNval, RNUnits, band):
        """Renormalize the spectrum to the specified value (in the specified
        flux units) in the specified band.
        Calls a function in another module to alleviate circular import
        issues."""

        from renorm import StdRenorm
        return StdRenorm(self,band,RNval,RNUnits)

class CompositeSourceSpectrum(SourceSpectrum):
    '''Composite Source Spectrum object, handles addition, multiplication
    and keeping track of the wavelength set.
    '''
    def __init__(self, source1, source2, operation):
        self.component1 = source1
        self.component2 = source2
        self.operation = operation
        self.name=str(self)

        # for now we keep these attributes here, in spite of the internal
        # units model. There is code that still breaks down if these attributes
        # are not here.
        try:
            self.waveunits = source1.waveunits
            self.fluxunits = source1.fluxunits
        except AttributeError:
            self.waveunits = source2.waveunits
            self.fluxunits = source2.fluxunits


    def __str__(self):
        opdict = {'add':'+','multiply':'*'}
        return "%s %s %s"%(str(self.component1),opdict[self.operation],str(self.component2))
    
    def __call__(self, wavelength):
        '''Add or multiply components, delegating the function calculation
        to the individual objects.
        '''
        if self.operation == 'add':
            return self.component1(wavelength) + self.component2(wavelength)
        if self.operation == 'multiply':
            return self.component1(wavelength) * self.component2(wavelength)

    def GetWaveSet(self):
        '''Obtain the wavelength set for the composite source by forming
        the union of wavelengths from each component.
        '''
        waveset1 = self.component1.GetWaveSet()
        waveset2 = self.component2.GetWaveSet()

        return MergeWaveSets(waveset1, waveset2)

class TabularSourceSpectrum(SourceSpectrum):
    '''Class for a source spectrum that is read in from a table.
    '''
    def __init__(self, filename=None, fluxname=None, keepneg=False):
        if filename:
            self._readSpectrumFile(filename, fluxname)
            self.filename=filename
            self.validate_units() 
            self.validate_wavetable()
            if not keepneg:
                self.validate_fluxtable()
            self.ToInternal()
            self.name=self.filename

        else:
            self._wavetable = None
            self._fluxtable = None
            self.waveunits = None
            self.fluxunits = None
            self.filename = None
            self.name=self.filename
        
    def _reverse_wave(self):
        self._wavetable = self._wavetable[::-1]

        
    def __str__(self):
        return str(self.name)

    def _readSpectrumFile(self, filename, fluxname):
        if filename.endswith('.fits') or filename.endswith('.fit'):
            self._readFITS(filename, fluxname)
        else:
            self._readASCII(filename)

    def _readFITS(self, filename, fluxname):
        fs = pyfits.open(filename)

        self._wavetable = fs[1].data.field('wavelength')
        if fluxname == None:
            fluxname = 'flux'
        self._fluxtable = fs[1].data.field(fluxname)

        self.waveunits = units.Units(fs[1].header['tunit1'].lower())
        self.fluxunits = units.Units(fs[1].header['tunit2'].lower())

        fs.close()

    def _readASCII(self, filename):
        """ Ascii files have no headers. Following synphot, this
        routine will assume the first column is wavelength in Angstroms,
        and the second column is flux in Flam."""

        self.waveunits = units.Units('angstrom')
        self.fluxunits = units.Units('flam')
        wlist,flist = self._columnsFromASCII(filename)
        self._wavetable=N.array(wlist,dtype=N.float64)
        self._fluxtable=N.array(flist,dtype=N.float64)
                

    def __call__(self, wavelengths):
        '''This is where the flux array is actually calculated given a
        wavelength array. Returns an array of flux values calculated at
        the wavelength values input.
        '''
        return self.resample(wavelengths)._fluxtable

    def taper(self):
        '''Taper the spectrum by adding zeros to each end.
        '''
        OutSpec = TabularSourceSpectrum()
        wcopy = N.zeros(self._wavetable.size+2,dtype=N.float64)
        fcopy = N.zeros(self._fluxtable.size+2,dtype=N.float64)
        wcopy[1:-1] = self._wavetable
        fcopy[1:-1] = self._fluxtable
        fcopy[0] = 0.0
        fcopy[-1] = 0.0

        ## The wavelengths to use for the first and last points are
        ## calculated by using the same ratio as for the 2 interior points
        wcopy[0] = wcopy[1]*wcopy[1]/wcopy[2]
        wcopy[-1] = wcopy[-2]*wcopy[-2]/wcopy[-3]

        OutSpec._wavetable = wcopy
        OutSpec._fluxtable = fcopy
        OutSpec.waveunits = units.Units(str(self.waveunits))
        OutSpec.fluxunits = units.Units(str(self.fluxunits))

        return OutSpec
        
    def resample(self, resampledWaveTab):
        '''Interpolate flux given a wavelength array that is monotonically
        increasing and the TabularSourceSpectrum object.
        @param resampledWaveTab: new wavelength table IN ANGSTROMS
        @type ressampledWaveTab: ndarray
        '''
        
        ##Check whether the input wavetab is in descending order
        if resampledWaveTab[0]<resampledWaveTab[-1]:
            newwave=resampledWaveTab
            newasc = True
        else:
            newwave=resampledWaveTab[::-1]
            newasc = False

        ## First need to pad the ends of the spectrum with zeros
        tapered = self.taper()

        ## Use numpy interpolation function
        if tapered._wavetable[0]<tapered._wavetable[-1]:
            oldasc = True
            ans = N.interp(newwave,tapered._wavetable,
                           tapered._fluxtable)
        else:
            oldasc = False
            rev = N.interp(newwave,tapered._wavetable[::-1],
                           tapered._fluxtable[::-1])
            ans = rev[::-1]

 
        ## If the new and old waveset don't have the same parity,
        ## the answer has to be flipped again
        if (newasc != oldasc):
            ans=ans[::-1]

        ## Finally, make the new object
        # NB: these manipulations were done using the internal
        #tables in Angstrom and photlam, so those are the units
        #that must be fed to the constructor. 
        resampled=ArraySourceSpectrum(wave=resampledWaveTab.copy(),
                                      waveunits = 'angstroms',
                                      flux = ans.copy(),
                                      fluxunits = 'photlam',
                                      keepneg=True)

        #Use the convert method to set the units desired by the user.
        resampled.convert(self.waveunits)
        resampled.convert(self.fluxunits)

        return resampled

    def GetWaveSet(self):
        '''For a TabularSource Spectrum, the WaveSet is just the _wavetable
        member.  Return a copy so that there is no reference to the original
        object.
        '''
        return self._wavetable.copy()

    def ToInternal(self):
        '''Convert to the internal representation of (angstroms, photlam).
        '''
        self.validate_units()
        savewunits = self.waveunits
        savefunits = self.fluxunits
        angwave = self.waveunits.Convert(self.GetWaveSet(), 'angstrom')
        phoflux = self.fluxunits.Convert(angwave, self._fluxtable, 'photlam')
        self._wavetable = angwave.copy()
        self._fluxtable = phoflux.copy()
        self.waveunits = savewunits
        self.fluxunits = savefunits

class ArraySourceSpectrum(TabularSourceSpectrum):
    """ spec = ArraySpectrum(numpy array containing wavelenght table,
    numpy array containing flux table, waveunits, fluxunits,
    name=human-readable nickname for spectrum, keepneg=True to
    override the default behavior of setting negative flux values to zero)
    """
    def __init__(self, wave=None, flux=None,
                 waveunits='angstrom', fluxunits='photlam',
                 name='UnnamedArraySpectrum',
                 keepneg=False):
        """Create a spectrum from arrays.
        @param wave: Wavelength array
        @param flux: Flux array
        @type wave,flux:  Numpy array with numerical data

        @param waveunits: Units of wave
        @param fluxunits: Units of flux
        @type waveunits:  L{units.WaveUnits} or subclass
        @type fluxunits: L{units.FluxUnits} or subclass

        @param name: Description of this array
        @type name: string
        @param keepneg: If true, negative flux values will be retained; by default, they are forced to zero
        @type keepneg: bool
        """
        if len(wave)!=len(flux):
            raise ValueError("wave and flux arrays must be of equal length")
        
        self._wavetable=wave
        self._fluxtable=flux
        self.waveunits=units.Units(waveunits)
        self.fluxunits=units.Units(fluxunits)
        self.name=name

        self.validate_units() #must do before validate_fluxtable because it tests against unit type
        self.validate_wavetable() #must do before ToInternal in case of descending
        if not keepneg:
            self.validate_fluxtable()
            
        self.ToInternal()

    
class FileSourceSpectrum(TabularSourceSpectrum):
    """spec = FileSpectrum(filename (FITS or ASCII),
    fluxname=column name containing flux (for FITS tables only),
    keepneg=True to override thedefault behavior of setting negative
    flux values to zero)"""

    def __init__(self, filename, fluxname=None, keepneg=False):
        """Create a spectrum from a file.
        @param filename: FITS or ASCII file containing the spectrum
        @type filename: string

        @param fluxname: Column name specifying the flux (FITS only)
        @type fluxname: string

        @param keepneg: If true, negative flux values will be retained; by default, they are forced to zero
        @type keepneg: bool
 
        """
        self._readSpectrumFile(filename, fluxname)
        self.name=filename
        self.validate_units() 
        self.validate_wavetable()
        if not keepneg:
            self.validate_fluxtable()
        self.ToInternal()

    def _readSpectrumFile(self, filename, fluxname):
        if filename.endswith('.fits') or filename.endswith('.fit'):
            self._readFITS(filename, fluxname)
        else:
            self._readASCII(filename)

    def _readFITS(self, filename, fluxname):
        fs = pyfits.open(filename)
        
        self._wavetable = fs[1].data.field('wavelength')
        if fluxname == None:
            fluxname = 'flux'
        self._fluxtable = fs[1].data.field(fluxname)
        self.waveunits = units.Units(fs[1].header['tunit1'].lower())
        self.fluxunits = units.Units(fs[1].header['tunit2'].lower())
        fs.close()

    def _readASCII(self, filename):
        """ Ascii files have no headers. Following synphot, this
        routine will assume the first column is wavelength in Angstroms,
        and the second column is flux in Flam."""

        self.waveunits = units.Units('angstrom')
        self.fluxunits = units.Units('flam')
        wlist,flist = self._columnsFromASCII(filename)
        self._wavetable=N.array(wlist,dtype=N.float64)
        self._fluxtable=N.array(flist,dtype=N.float64)
            
                                
                                                        
class AnalyticSpectrum(SourceSpectrum):
    ''' Base class for analytic functions. These are spectral forms
    which are defined, by default, on top of the default synphot
    waveset. 
    '''

    def __init__(self,waveunits='angstrom',fluxunits='photlam'):
        "All AnalyticSpectra must set wave & flux units; do it here."
        self.waveunits = units.Units(waveunits)
        self.fluxunits = units.Units(fluxunits)
        self.validate_units()
        
    def GetWaveSet(self):
        global default_waveset
        return default_waveset.copy()


class GaussianSource(AnalyticSpectrum):
    """spec = GaussianSource(TotalFlux under Gaussian, central wavelength of
    Gaussian, FWHM of Gaussian, waveunits, fluxunits)
    """
    def __init__(self, flux, center, fwhm, waveunits='angstrom',
                 fluxunits='flam'):
        AnalyticSpectrum.__init__(self,waveunits,fluxunits)
        self.center = center
        self.input_fwhm = fwhm
        self.input_flux = flux
        self._input_units = self.fluxunits
        self.sigma = fwhm / math.sqrt(8.0 * math.log(2.0))
        self.factor = flux / (math.sqrt(2.0 * math.pi) * self.sigma)
        self.name ='Gaussian: mu=%f,fwhm=%f,flux=%f %s'%(self.center,self.input_fwhm,self.input_flux,self._input_units)

    def __str__(self):
        return self.name

    def __call__(self, wavelength):
        sp = TabularSourceSpectrum()
        sp.waveunits = self.waveunits
        sp.fluxunits = self._input_units
        sp._wavetable = wavelength
        sp._fluxtable = self.factor * N.exp( \
            -0.5 *((wavelength - self.center)/ self.sigma)**2)
        sp.ToInternal()

        return sp(wavelength)

    def GetWaveSet(self):
        '''Return a wavelength set that describes the Gaussian.
        Overrides the base class to compute 101 values, from
        center - 5*sigma to center + 5*sigma, in units of
        0.1*sigma
        '''
        increment = 0.1*self.sigma
        first = self.center - 50.0*increment
        last = self.center + 50.0*increment
        return N.arange(first, last, increment)


class FlatSpectrum(AnalyticSpectrum):
    """spec = FlatSpectrum(Flux density, waveunits, fluxunits). Defines a
    flat spectrum in units of fluxunits."""
    def __init__(self, fluxdensity, waveunits='angstrom', fluxunits='photlam'):
        AnalyticSpectrum.__init__(self,waveunits,fluxunits)
        self.wavelength = None
        self._fluxdensity = fluxdensity
        self._input_units = self.fluxunits
        self.name="Unit spectrum of %f %s"%(self._fluxdensity,self._input_units)
    def __str__(self):
        return self.name
    
    def __call__(self, wavelength):
        """Create a TabularSourceSpectrum, then use its __call__"""
        sp = TabularSourceSpectrum()
        sp.waveunits = self.waveunits
        sp.fluxunits = self._input_units
        sp._wavetable = wavelength
        sp._fluxtable = self._fluxdensity*N.ones(sp._wavetable.shape,
                                                 dtype=N.float64) 
        sp.ToInternal()
        return sp(wavelength)

    def redshift(self, z):
        """Call the parent's method, which returns a TabularSourceSpectrum,
        then use its results to create a new FlatSpectrum with the correct
        value. """
        
        tmp=SourceSpectrum.redshift(self,z)
        ans=FlatSpectrum(tmp.flux.max(),
                         fluxunits=tmp.fluxunits)
        return ans

##This change produces 5 errors and 17 failures in cos_etc_test.py
##     def GetWaveSet(self):
##         global default_waveset
##         return N.array([default_waveset[0],default_waveset[-1]])


class Powerlaw(AnalyticSpectrum):
    """spec=PowerLaw(refwave, exponent, waveunits, fluxunits). As described in U{http://www.stsci.edu/resources/software_hardware/stsdas/synphot/SynphotManual.pdf}, table 3.5"""
    def __init__(self, refwave, index, waveunits='angstrom', fluxunits='photlam'):
        AnalyticSpectrum.__init__(self,waveunits,fluxunits)
        self.wavelength = None
        self._input_units = self.fluxunits
        self._refwave = refwave
        self._index = index
        self.name="Power law: refwave %f, index %f"%(self._refwave,self._index)
        
    def __str__(self):
        return self.name

    def __call__(self, wavelength):
        sp = TabularSourceSpectrum()
        sp.waveunits = self.waveunits
        sp.fluxunits = self._input_units
        sp._wavetable = wavelength
        sp._fluxtable = N.ones(sp._wavetable.shape, dtype=N.float64)

        for i in range(len(sp._fluxtable)):
            sp._fluxtable[i] = (sp._wavetable[i] / self._refwave) ** self._index

        sp.ToInternal()

        return sp(wavelength)


class BlackBody(AnalyticSpectrum):
    """ spec = BlackBody(T in Kelvin)"""
    def __init__(self, temperature):
        self.waveunits=units.Units('angstrom')
        self.fluxunits=units.Units('photlam')
        self.wavelength = None
        self.temperature = temperature
        self.name='BB(T=%d)'%self.temperature
        
    def __str__(self):
        return self.name

    def __call__(self, wavelength):
        sp = TabularSourceSpectrum()
        sp.waveunits = self.waveunits
        sp.fluxunits = self.fluxunits
        sp._wavetable = wavelength

        sp._fluxtable = planck.bbfunc(wavelength, self.temperature)* RENORM

        return sp(wavelength)


class SpectralElement(Integrator):
    '''Base class for a Spectral Element (e.g. Filter, Detector...).
    '''
    def validate_units(self):
        "Ensure that waveunits are WaveUnits"
        if (not isinstance(self.waveunits,units.WaveUnits)):
            raise TypeError("%s is not a valid WaveUnit"%self.waveunits)

    def __mul__(self, other):
        '''Permitted to multiply a SpectralElement by another
        SpectralElement, or by a SourceSpectrum.  In the former
        case we return a CompositeSpectralElement, while in the
        latter case a CompositeSourceSpectrum.
        '''
        if isinstance(other, SpectralElement):
            return CompositeSpectralElement(self, other)

        if isinstance(other, SourceSpectrum):
            return CompositeSourceSpectrum(self, other, 'multiply')

        ## Multiplying by a constant is the same as multiplying by a
        ## UniformTransmission object
        if type(other) in [type(1), type(1.0)]:
            return CompositeSpectralElement(self, UniformTransmission(other))

        else:
            print "SpectralElements can only be multiplied by other " + \
                  "SpectralElements or SourceSpectrum objects"

    def __rmul__(self, other):
        return self.__mul__(other)


    def convert(self, targetunits):
        '''Convert to other units. This method actually just changes the
        wavelength unit objects, it does not recompute the
        internally kept wave data; these are kept always in internal
        units. Method getWaveSet does the actual computation.'''
        nunits = units.Units(targetunits)
        self.waveunits = nunits

    
    def ToInternal(self):
        '''Convert wavelengths to the internal representation of angstroms..
        Note: This is not yet used, but should be for safety when creating
        TabularSpectralElements from files. It will also be necessary for the
        ArraySpectralElement class that we want to create RSN.
        '''
        self.validate_units()
        savewunits = self.waveunits
        angwave = self.waveunits.Convert(self.GetWaveSet(), 'angstrom')
        self._wavetable = angwave.copy()
        self.waveunits = savewunits


    def __call__(self, wavelengths):
        '''This is where the throughput array is calculated for a given
        input wavelength table.
        '''
        return self.resample(wavelengths)._throughputtable

    def taper(self):
        '''Taper the spectrum by adding zeros to each end.
        '''
        OutElement = TabularSpectralElement()

        wcopy = N.zeros(self._wavetable.size+2,dtype=N.float64)
        fcopy = N.zeros(self._throughputtable.size+2,dtype=N.float64)

        wcopy[1:-1] = self._wavetable
        fcopy[1:-1] = self._throughputtable

        fcopy[0] = 0.0
        fcopy[-1] = 0.0

        ## The wavelengths to use for the first and last points are
        ## calculated by using the same ratio as for the 2 interior points
        wcopy[0] = wcopy[1]*wcopy[1]/wcopy[2]
        wcopy[-1] = wcopy[-2]*wcopy[-2]/wcopy[-3]

        OutElement._wavetable = wcopy
        OutElement._throughputtable = fcopy

        return OutElement
    
    def writefits(self, filename, clobber=True, trimzero=True):
        """Write the bandpass to a FITS file."""

        if clobber:
            try:
                os.remove(filename)
            except OSError:
                pass
            
        wave=self.wave
        thru=self.throughput
            
        first,last=0,len(thru)
        if trimzero:
            #Keep one zero at each end
            nz = thru.nonzero()[0]
            try:
                first=max(nz[0]-1,first)
                last=min(nz[-1]+2,last)
            except IndexError:
                pass

        #Construct the columns and HDUlist
        cw = pyfits.Column(name='WAVELENGTH',
                           array=wave[first:last],
                           unit=self.waveunits.name,
                           format='E')
        cf = pyfits.Column(name='THROUGHPUT',
                           array=thru[first:last],
                           unit='         ',
                           format='E')
        hdu = pyfits.PrimaryHDU()
        hdulist = pyfits.HDUList([hdu])

        #Write the file
        cols = pyfits.ColDefs([cw, cf])
        hdu = pyfits.new_table(cols)
        hdulist.append(hdu)
        hdu.writeto(filename)
                                                 
    def resample(self, resampledWaveTab):
        '''Interpolate throughput given a wavelength array that is
        monotonically increasing and the TabularSpectralElement object.'''
        ##Check whether the input wavetab is in descending order

        if resampledWaveTab[0]<resampledWaveTab[-1]:
            newwave=resampledWaveTab
            newasc = True
        else:
            newwave=resampledWaveTab[::-1]
            newasc = False

        #First need to pad the ends with zeros(?)
        tapered=self.taper()
        ## Use numpy interpolation function
        if tapered._wavetable[0]<tapered._wavetable[-1]:
            oldasc = True
            ans = N.interp(newwave,tapered._wavetable,
                           tapered._throughputtable)
        else:
            oldasc = False
            rev = N.interp(newwave,tapered._wavetable[::-1],
                           tapered._throughputtable[::-1])
            ans = rev[::-1]

        ## If the new and old waveset don't have the same parity,
        ## the answer has to be flipped again
        if (newasc != oldasc):
            ans=ans[::-1]

        # Finally, make the new object.
        # NB: these manipulations were done using the internal
        #tables in Angstrom, so those are the units
        #that must be fed to the constructor. 
        resampled=ArraySpectralElement(wave=resampledWaveTab.copy(),
                                       waveunits = 'angstroms',
                                       throughput = ans.copy())
        #Use the convert method to set the units desired by the user.
        resampled.convert(self.waveunits)

        return resampled

    def unitResponse(self):
        """Is this correct if waveunits != Angstrom?"""
        wave = self.GetWaveSet()
        thru = self(wave)
        return 1.0 / self.trapezoidIntegration(wave,thru)
    
        

    def GetWaveSet(self):
        "Return the waveset in the requested units."
        wave = units.Angstrom().Convert(self._wavetable, self.waveunits.name)
        return wave

    def GetThroughput(self):
        """Return the throughput for the internal wavetable"""
        return self.__call__(self.wave)
    
    wave = property(GetWaveSet, doc='Waveset for bandpass')
    throughput = property(GetThroughput, doc='Throughput for bandpass')


class CompositeSpectralElement(SpectralElement):
    '''CompositeSpectralElement Class, which knows how to calculate
    its throughput by delegating the calculating the calculating to
    its components.
    '''
    def __init__(self, component1, component2):
        if (not isinstance(component1, SpectralElement) or
            not isinstance(component2, SpectralElement)):
            raise TypeError("Arguments must be SpectralElements")
        self.component1 = component1
        self.component2 = component2
        if component1.waveunits.name == component2.waveunits.name:
            self.waveunits = component1.waveunits
        else:
            msg="Components have different waveunits (%s and %s)"%(component1.waveunits,component2.waveunits)
            raise NotImplementedError(msg)
        self.throughputunits = None
        self.name="(%s * %s)"%(str(self.component1),str(self.component2))
        
    def __call__(self, wavelength):
        '''This is where the throughput calculation is delegated.
        '''
        return self.component1(wavelength) * self.component2(wavelength)

    def __str__(self):
        return self.name

    def GetWaveSet(self):
        '''This method returns a wavelength set appropriate for a composite
        object by forming the union of the wavelengths of the components.
        '''
        wave1 = self.component1.GetWaveSet()
        wave2 = self.component2.GetWaveSet()

        return MergeWaveSets(wave1, wave2)

    wave = property(GetWaveSet,doc="wave for CompositeSpectralElement")


class UniformTransmission(SpectralElement):
    '''bandpass=UniformTransmission(dimensionless throughput)
    
    @todo: Need to add a GetWaveSet method (or just return None).
    '''
    def __init__(self, value, waveunits='angstrom'):
        self.waveunits = units.Units(waveunits)
        self.value = value
        self.name="Uniform %5.3f"%value
        #The ._wavetable is used only by the .writefits() method at this time
        #It is not for general use.
        self._wavetable = N.array([default_waveset[0],default_waveset[-1]])

    def GetWaveSet(self):
        return None

## This produced 15 test failures in cos_etc_test.
##     def GetWaveSet(self):
##         global default_waveset
##         return N.array([default_waveset[0],default_waveset[-1]])
##
##     wave = property(GetWaveSet,doc="wave for UniformTransmission")
    
    def __call__(self, wavelength):
        '''__call__ returns the constant value as an array, given a
        wavelength array as argument.
        '''
        return 0.0 * wavelength + self.value


class TabularSpectralElement(SpectralElement):
    """bandpass = FileBandpass(FITS or ASCII filename, thrucol= name of
    column containing throughput values (for FITS tables only)
    """
    def __init__(self, fileName=None, thrucol='throughput'):
        '''__init__ takes a character string argument that contains the name
        of the file with the spectral element table.
        '''
        if fileName:
            if fileName.endswith('.fits') or fileName.endswith('.fit'):
                self._readFITS(fileName, thrucol)
            else:
                self._readASCII(fileName)
            self.name = fileName

        else:
            self.name = None
            self._wavetable = None
            self._throughputtable = None
            self.waveunits = None
            self.throughputunits = None

    def _reverse_wave(self):
        self._wavetable = self._wavetable[::-1]
        
    def __str__(self):
        return str(self.name)


    def _readASCII(self,filename):
        """ Ascii files have no headers. Following synphot, this
        routine will assume the first column is wavelength in Angstroms,
        and the second column is throughput (dimensionless)."""
        self.waveunits = units.Units('angstrom')
        self.throughputunits = 'none'
        wlist,tlist = self._columnsFromASCII(filename)
        self._wavetable=N.array(wlist,dtype=N.float64)
        self._throughputtable=N.array(tlist,dtype=N.float64)
       

    def _readFITS(self,filename,thrucol='throughput'):
        fs = pyfits.open(filename)
        
        self._wavetable = fs[1].data.field('wavelength')
        self._throughputtable = fs[1].data.field(thrucol)
        
        self.waveunits = units.Units(fs[1].header['tunit1'].lower())
        self.throughputunits = 'none'
        
        self.getHeaderKeywords(fs[1].header)
        
        fs.close()

       
    def getHeaderKeywords(self, header):
        ''' This is a placeholder for subclasses to get header keywords without
        having to reopen the file again. 
        '''
        pass


class ArraySpectralElement(TabularSpectralElement):
    """ spec = ArraySpectrum(numpy array containing wavelength table,
    numpy array containing throughput table, waveunits, 
    name=human-readable nickname for bandpass.
    """
    def __init__(self, wave=None, throughput=None,
                 waveunits='angstrom', 
                 name='UnnamedArrayBandpass'):

        """Create a spectrum from arrays.
        @param wave: Wavelength array
        @param throughput: Throughput array
        @type wave,throughput:  Numpy array with numerical data

        @param waveunits: Units of wave
        @type waveunits:  L{units.WaveUnits} or subclass

        @param name: Description of this spectral element
        @type name: string
        """
        if len(wave)!=len(throughput):
            raise ValueError("wave and throughput arrays must be of equal length")
        
        self._wavetable=wave
        self._throughputtable=throughput
        self.waveunits=units.Units(waveunits)
        self.name=name

        self.validate_units() #must do before validate_fluxtable because it tests against unit type
        self.validate_wavetable() #must do before ToInternal in case of descending
            
        self.ToInternal()

    
class FileSpectralElement(TabularSpectralElement):
    """spec = FileSpectrum(filename (FITS or ASCII),
    throughputname=column name containing throughput (for FITS tables only),
    keepneg=True to override the default behavior of setting negative
    throughput values to zero)"""

    def __init__(self, filename, thrucol=None):
        """Create a bandpass from a file.
        @param filename: FITS or ASCII file containing the bandpass
        @type filename: string

        @param thrucol: Column name specifying the throughput (FITS only)
        @type thrucol: string

        """
        self._readThroughputFile(filename, thrucol)
        self.name=filename
        self.validate_units() 
        self.validate_wavetable()
        self.ToInternal()

    def _readThroughputFile(self, filename, throughputname):
        if filename.endswith('.fits') or filename.endswith('.fit'):
            self._readFITS(filename, throughputname)
        else:
            self._readASCII(filename)

    def _readFITS(self, filename, throughputname):
        fs = pyfits.open(filename)
        
        self._wavetable = fs[1].data.field('wavelength')
        if throughputname == None:
            throughputname = 'throughput'
        self._throughputtable = fs[1].data.field(throughputname)
        self.waveunits = units.Units(fs[1].header['tunit1'].lower())
        fs.close()

    def _readASCII(self, filename):
        """ Ascii files have no headers. Following synphot, this
        routine will assume the first column is wavelength in Angstroms,
        and the second column is throughput in Flam."""

        self.waveunits = units.Units('angstrom')
        wlist,flist = self._columnsFromASCII(filename)
        self._wavetable=N.array(wlist,dtype=N.float64)
        self._throughputtable=N.array(flist,dtype=N.float64)
            
                                

class InterpolatedSpectralElement(SpectralElement):
    '''The InterpolatedSpectralElement class handles spectral elements
    that are interpolated from columns stored in FITS tables
    '''
    def __init__(self, fileName, wavelength):
        ''' The file name contains a suffix with a column name specification
        in between square brackets, such as [fr388n#]. The wavelength
        parameter (poorly named -- it is not always a wavelength) is used to
        interpolate between two columns in the file.
        '''
        self.name = fileName.split('[')[0]
        colSpec = fileName.split('[')[1][:-1]

        self.interpval = wavelength

        fs = pyfits.open(self.name)

        #The wavelength table will have to be adjusted before use
        wave0 = fs[1].data.field('wavelength')
        

        #Determine the columns that bracket the desired value
        colNames = fs[1].data.names[2:]
        colWaves = []
        for columnName in colNames:
            colWaves.append(float(columnName.split('#')[1]))

        waves = MA.array(colWaves)
        greater = MA.masked_less(waves, wavelength)
        less = MA.masked_greater(waves, wavelength)
        upper = MA.minimum(greater)
        lower = MA.maximum(less)

        if '--' in (str(upper),str(lower)):
            raise NotImplementedError("%f outside of range in %s; extrapolation not yet supported"%(wavelength,fileName))


        lcol = (colSpec + str(lower)).upper()
        ucol = (colSpec + str(upper)).upper()

        

        #Extract the data from those columns
        lthr = fs[1].data.field(lcol)
        uthr = fs[1].data.field(ucol)

        if upper != lower:
            #Adjust the wavelength table to bracket the range
            lwave = wave0 + (lower-self.interpval)
            uwave = wave0 + (upper-self.interpval)

            #Interpolate the columns at those ranges
            lthr = N.interp(lwave, wave0, fs[1].data.field(lcol))
            uthr = N.interp(uwave, wave0, fs[1].data.field(ucol))

            #Then interpolate between the two columns
            w = (wavelength - lower) / (upper - lower)
            self._throughputtable = uthr * w + lthr * (1.0 - w)
        else:
            #Interpolate the matching column to the correct wave range
            uwave = wave0 + (upper-self.interpval)
            uthr = N.interp(uwave, wave0, fs[1].data.field(ucol))
            self._throughputtable = uthr
            
        self._wavetable = wave0
        self.waveunits = units.Units(fs[1].header['tunit1'].lower())
        self.throughputunits = 'none'

        fs.close()

    def __str__(self):
        return "%s#%f"%(self.name,self.interpval)


class ThermalSpectralElement(TabularSpectralElement):
    '''The ThermalSpectralElement class handles spectral elements
    that have associated thermal properties read from a FITS table.

    ThermalSpectralElements differ from regular SpectralElements in
    that they carry thermal parameters such as temperature and beam
    filling factor, but otherwise they operate just as regular
    SpectralElements. They dont know how to apply themselves to an
    existing beam, in the sense that their emissivities should be
    handled explictly, outside the objects themselves.
    '''
    def __init__(self, fileName):

        TabularSpectralElement.__init__(self, fileName=fileName, thrucol='emissivity')

    def getHeaderKeywords(self, header):
        ''' Overrides base class in order to get thermal keywords.
        '''
        self.temperature = header['DEFT']
        self.beamFillFactor = header['BEAMFILL']


class Box(SpectralElement):
    """bandpass = Box(central wavelength, width) - both in Angstroms"""
    def __init__(self, center, width):
        ''' Both center and width are assumed to be in Angstrom
            units, according to the synphot definition.
        '''
        self.waveunits=units.Units('angstrom') #per docstring: for now
        lower = center - width / 2.0
        upper = center + width / 2.0
        step = 0.05                     # fixed step for now (in A)

        self.name='Box at %f (%f wide)'%(center,width)
        nwaves = int(((upper - lower) / step)) + 2
        self._wavetable = N.zeros(shape=[nwaves,], dtype=N.float64)
        for i in range(nwaves):
            self._wavetable[i] = lower + step * i

        self._wavetable[0]  = self._wavetable[1]  - step
        self._wavetable[-1] = self._wavetable[-2] + step
        
        self._throughputtable = N.ones(shape=self._wavetable.shape, \
                                        dtype=N.float64)
        self._throughputtable[0]  = 0.0
        self._throughputtable[-1] = 0.0

        
        

Vega = FileSourceSpectrum(locations.VegaFile)
